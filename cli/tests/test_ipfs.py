from __future__ import annotations

import json

import httpx
import pytest

from kswarm_cli.ipfs import (
    DEFAULT_IPFS_API_URL,
    DEFAULT_IPFS_MAX_BYTES,
    IPFS_MAX_BYTES_ENV,
    IpfsError,
    add_bytes,
    api_url,
    cat_bytes,
    cat_json,
    check,
    max_artifact_bytes,
)


def _client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_api_url_precedence_and_trailing_slash() -> None:
    assert api_url(None, {}) == DEFAULT_IPFS_API_URL == "http://127.0.0.1:5001"
    assert api_url("http://a:1/", {"KSWARM_IPFS_API_URL": "http://b:2"}) == "http://a:1"
    assert api_url(None, {"KSWARM_IPFS_API_URL": "http://b:2/", "PROTOCOL_IPFS_API_URL": "http://c:3"}) == "http://b:2"
    assert api_url(None, {"PROTOCOL_IPFS_API_URL": "http://c:3"}) == "http://c:3"
    assert api_url("  ", {"KSWARM_IPFS_API_URL": "  "}) == DEFAULT_IPFS_API_URL


def test_max_artifact_bytes_default_and_override() -> None:
    assert max_artifact_bytes({}) == DEFAULT_IPFS_MAX_BYTES == 8 * 1024 * 1024
    assert max_artifact_bytes({IPFS_MAX_BYTES_ENV: "1024"}) == 1024
    for bad in ("0", "-5", "8MiB", "1.5"):
        with pytest.raises(IpfsError, match="positive integer"):
            max_artifact_bytes({IPFS_MAX_BYTES_ENV: bad})


def test_check_accepts_kubo_version_and_rejects_others() -> None:
    check("http://ipfs", client=_client(lambda request: httpx.Response(200, json={"Version": "0.40.1"})))
    with pytest.raises(IpfsError, match="IPFS_UNREACHABLE"):
        check("http://ipfs", client=_client(lambda request: httpx.Response(200, json={"hello": 1})))
    with pytest.raises(IpfsError, match="IPFS_UNREACHABLE"):
        check("http://ipfs", client=_client(lambda request: httpx.Response(500)))

    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    with pytest.raises(IpfsError, match="IPFS_UNREACHABLE"):
        check("http://ipfs", client=_client(down))


def test_add_bytes_pins_cid_v1_and_returns_last_hash_line() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["params"] = dict(request.url.params)
        seen["path"] = request.url.path
        return httpx.Response(200, text='{"Name":"x","Hash":"bafyfirst"}\n{"Name":"y","Hash":"bafylast","Size":"3"}\n')

    assert add_bytes("http://ipfs", "x.json", b"{}", client=_client(handler)) == "bafylast"
    assert seen["path"] == "/api/v0/add"
    assert seen["params"] == {"pin": "true", "cid-version": "1"}
    with pytest.raises(IpfsError, match="IPFS_ADD_FAILED"):
        add_bytes("http://ipfs", "x.json", b"{}", client=_client(lambda request: httpx.Response(200, text="")))
    with pytest.raises(IpfsError, match="IPFS_ADD_FAILED"):
        add_bytes("http://ipfs", "x.json", b"{}", client=_client(lambda request: httpx.Response(200, text="not json")))
    with pytest.raises(IpfsError, match="IPFS_ADD_FAILED"):
        add_bytes("http://ipfs", "x.json", b"{}", client=_client(lambda request: httpx.Response(500)))


def test_cat_bytes_reads_within_the_cap() -> None:
    payload = b"x" * 100
    client = _client(lambda request: httpx.Response(200, content=payload))
    assert cat_bytes("http://ipfs", "bafy", max_bytes=100, client=client) == payload
    assert cat_json("http://ipfs", "bafy", max_bytes=64, client=_client(lambda request: httpx.Response(200, content=b'{"a": 1}'))) == {"a": 1}


def test_cat_bytes_refuses_oversized_artifacts_by_declared_length() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"x" * 10, headers={"content-length": "10"})

    with pytest.raises(IpfsError, match=r"IPFS_ARTIFACT_TOO_LARGE: bafy is over 9 bytes.*KSWARM_IPFS_MAX_BYTES"):
        cat_bytes("http://ipfs", "bafy", max_bytes=9, client=_client(handler))


def test_cat_bytes_refuses_oversized_streams_without_content_length() -> None:
    def chunks():
        for _ in range(4):
            yield b"abcd"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=httpx.ByteStream(b"".join(chunks())), headers={"transfer-encoding": "chunked"})

    with pytest.raises(IpfsError, match="IPFS_ARTIFACT_TOO_LARGE"):
        cat_bytes("http://ipfs", "bafy", max_bytes=15, client=_client(handler))
    assert cat_bytes("http://ipfs", "bafy", max_bytes=16, client=_client(handler)) == b"abcd" * 4


def test_cat_json_reports_non_json_and_http_errors() -> None:
    with pytest.raises(IpfsError, match="IPFS_ARTIFACT_NOT_JSON"):
        cat_json("http://ipfs", "bafy", max_bytes=64, client=_client(lambda request: httpx.Response(200, content=b"\xff\xfe")))
    with pytest.raises(IpfsError, match="IPFS_CAT_FAILED"):
        cat_json("http://ipfs", "bafy", max_bytes=64, client=_client(lambda request: httpx.Response(404)))
    with pytest.raises(IpfsError, match="positive"):
        cat_bytes("http://ipfs", "bafy", max_bytes=0, client=_client(lambda request: httpx.Response(200)))
    assert json.dumps({"ok": True})
