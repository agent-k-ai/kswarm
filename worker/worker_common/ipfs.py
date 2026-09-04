from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any

import httpx


# Kubo's default API port. This constant is the single source of truth for the
# worker stack; `worker_common.config` reads it through `default_api_url()`.
DEFAULT_IPFS_API_URL = "http://127.0.0.1:5001"


class IpfsError(RuntimeError):
    pass


@dataclass
class IpfsClient:
    api_url: str | None = None
    timeout_seconds: float = 60.0
    transport: httpx.BaseTransport | None = None

    def __post_init__(self) -> None:
        self.api_url = (self.api_url or default_api_url()).rstrip("/")
        self._client = httpx.Client(timeout=self.timeout_seconds, transport=self.transport)

    def check(self) -> None:
        response = self._client.post(f"{self.api_url}/api/v0/version")
        if response.status_code != 200:
            raise IpfsError(f"IPFS_UNREACHABLE: {self.api_url} returned {response.status_code}")
        try:
            payload = response.json()
        except ValueError as exc:
            raise IpfsError(f"IPFS_UNREACHABLE: {self.api_url} did not return IPFS JSON") from exc
        if "Version" not in payload and "version" not in payload:
            raise IpfsError(f"IPFS_UNREACHABLE: {self.api_url} did not return an IPFS version payload")

    def add_bytes(self, filename: str, payload: bytes) -> str:
        response = self._client.post(
            f"{self.api_url}/api/v0/add",
            params={"pin": "true", "cid-version": "1"},
            files={"file": (filename, payload)},
        )
        if response.status_code != 200:
            raise IpfsError(f"ipfs add failed: {response.status_code} {response.text}")
        lines = [line.strip() for line in response.text.splitlines() if line.strip()]
        if not lines:
            raise IpfsError("ipfs add returned an empty response")
        try:
            return str(json.loads(lines[-1])["Hash"])
        except (KeyError, ValueError) as exc:
            raise IpfsError(f"ipfs add returned malformed response: {lines[-1]}") from exc

    def add_json(self, filename: str, payload: Any) -> str:
        data = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return self.add_bytes(filename, data)

    def cat_bytes(self, cid: str) -> bytes:
        response = self._client.post(f"{self.api_url}/api/v0/cat", params={"arg": cid})
        if response.status_code != 200:
            raise IpfsError(f"ipfs cat failed for {cid}: {response.status_code} {response.text}")
        return response.content

    def cat_json(self, cid: str) -> Any:
        return json.loads(self.cat_bytes(cid).decode("utf-8"))


def default_api_url() -> str:
    return (
        os.environ.get("KSWARM_IPFS_API_URL")
        or os.environ.get("PROTOCOL_IPFS_API_URL")
        or DEFAULT_IPFS_API_URL
    )


def upload_bytes(data: bytes) -> str:
    return IpfsClient().add_bytes("artifact.bin", data)


def download_bytes(cid: str) -> bytes:
    return IpfsClient().cat_bytes(cid)
