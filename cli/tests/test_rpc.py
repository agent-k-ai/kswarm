from __future__ import annotations

import json

import httpx
import pytest

from kswarm_cli.rpc import MULTIPLE_ACCOUNTS_CHUNK, RpcClient, RpcError


def _client_with(handler) -> RpcClient:
    rpc = RpcClient("http://rpc", "confirmed")
    rpc._client = httpx.Client(transport=httpx.MockTransport(handler))
    return rpc


def test_get_multiple_account_infos_chunks_at_the_rpc_limit() -> None:
    requests: list[list[str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["method"] == "getMultipleAccounts"
        keys, options = body["params"]
        assert options == {"encoding": "base64", "commitment": "confirmed"}
        requests.append(keys)
        values = [None if key.endswith("0") else {"data": ["", "base64"], "owner": "x"} for key in keys]
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": {"context": {"slot": 1}, "value": values}})

    keys = [f"key{index}" for index in range(MULTIPLE_ACCOUNTS_CHUNK + 5)]
    result = _client_with(handler).get_multiple_account_infos(keys)
    assert [len(chunk) for chunk in requests] == [MULTIPLE_ACCOUNTS_CHUNK, 5]
    assert len(result) == len(keys)
    assert result[0] is None and result[10] is None and result[1] is not None
    assert MULTIPLE_ACCOUNTS_CHUNK == 100


def test_get_multiple_account_infos_empty_list_makes_no_request() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no request expected")

    assert _client_with(handler).get_multiple_account_infos([]) == []


def test_get_multiple_account_infos_rejects_short_responses() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": {"value": [None]}})

    with pytest.raises(RpcError, match="MalformedResponse|returned 1 of 2"):
        _client_with(handler).get_multiple_account_infos(["a", "b"])
