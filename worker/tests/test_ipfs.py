from __future__ import annotations

import json

import httpx
import pytest

from worker_common.ipfs import DEFAULT_IPFS_API_URL, IpfsClient, IpfsError, default_api_url


def test_ipfs_client_upload_and_download_bytes() -> None:
    stored = b"hello-ipfs"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v0/add":
            assert request.method == "POST"
            return httpx.Response(200, text=json.dumps({"Name": "artifact.bin", "Hash": "bafkreitest"}) + "\n")
        if request.url.path == "/api/v0/cat":
            assert request.url.params["arg"] == "bafkreitest"
            return httpx.Response(200, content=stored)
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    client = IpfsClient("http://ipfs.example:5001", transport=httpx.MockTransport(handler))

    assert client.add_bytes("artifact.bin", stored) == "bafkreitest"
    assert client.cat_bytes("bafkreitest") == stored


def test_ipfs_client_rejects_malformed_add_response() -> None:
    client = IpfsClient(
        "http://ipfs.example:5001",
        transport=httpx.MockTransport(lambda request: httpx.Response(200, text="not-json\n")),
    )

    with pytest.raises(IpfsError, match="malformed"):
        client.add_bytes("artifact.bin", b"payload")


def test_default_api_url_prefers_kswarm_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PROTOCOL_IPFS_API_URL", "http://protocol-ipfs:5001")
    monkeypatch.setenv("KSWARM_IPFS_API_URL", "http://kswarm-ipfs:5001")

    assert default_api_url() == "http://kswarm-ipfs:5001"


def test_default_api_url_is_the_single_kubo_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PROTOCOL_IPFS_API_URL", raising=False)
    monkeypatch.delenv("KSWARM_IPFS_API_URL", raising=False)

    assert default_api_url() == DEFAULT_IPFS_API_URL == "http://127.0.0.1:5001"
    assert IpfsClient(transport=httpx.MockTransport(lambda request: httpx.Response(500))).api_url == DEFAULT_IPFS_API_URL
