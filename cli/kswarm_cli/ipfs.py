"""Kubo HTTP API client used by the prediction commands.

Reads are capped: an artifact larger than `KSWARM_IPFS_MAX_BYTES` (default
8 MiB) is refused instead of read into memory.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from typing import Any

import httpx


DEFAULT_IPFS_API_URL = "http://127.0.0.1:5001"
IPFS_API_URL_ENVS = ("KSWARM_IPFS_API_URL", "PROTOCOL_IPFS_API_URL")
IPFS_MAX_BYTES_ENV = "KSWARM_IPFS_MAX_BYTES"
DEFAULT_IPFS_MAX_BYTES = 8 * 1024 * 1024
CHECK_TIMEOUT_SECONDS = 5.0
TRANSFER_TIMEOUT_SECONDS = 60.0


class IpfsError(RuntimeError):
    """The API was unreachable, refused the request, or an artifact broke a rule."""


def api_url(value: str | None, environ: Mapping[str, str] | None = None) -> str:
    """Explicit value, then `KSWARM_IPFS_API_URL`, then `PROTOCOL_IPFS_API_URL`, then the Kubo default."""

    env = os.environ if environ is None else environ
    for candidate in (value, *(env.get(name) for name in IPFS_API_URL_ENVS)):
        if candidate and candidate.strip():
            return candidate.strip().rstrip("/")
    return DEFAULT_IPFS_API_URL


def max_artifact_bytes(environ: Mapping[str, str] | None = None) -> int:
    """`KSWARM_IPFS_MAX_BYTES` as a positive integer, or the 8 MiB default."""

    env = os.environ if environ is None else environ
    raw = env.get(IPFS_MAX_BYTES_ENV, "").strip()
    if not raw:
        return DEFAULT_IPFS_MAX_BYTES
    try:
        value = int(raw)
    except ValueError as exc:
        raise IpfsError(f"{IPFS_MAX_BYTES_ENV} must be a positive integer; got {raw!r}") from exc
    if value <= 0:
        raise IpfsError(f"{IPFS_MAX_BYTES_ENV} must be a positive integer; got {raw!r}")
    return value


def check(url: str, client: httpx.Client | None = None) -> None:
    """Fail with `IPFS_UNREACHABLE` unless `/api/v0/version` answers like Kubo."""

    try:
        response = _post(client, f"{url}/api/v0/version", timeout=CHECK_TIMEOUT_SECONDS)
        response.raise_for_status()
        payload = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise IpfsError(f"IPFS_UNREACHABLE: {url}") from exc
    if not isinstance(payload, dict) or ("Version" not in payload and "version" not in payload):
        raise IpfsError(f"IPFS_UNREACHABLE: {url}")


def add_bytes(url: str, filename: str, payload: bytes, client: httpx.Client | None = None) -> str:
    """Pin `payload` as a CIDv1 and return the CID."""

    try:
        response = _post(
            client,
            f"{url}/api/v0/add",
            params={"pin": "true", "cid-version": "1"},
            files={"file": (filename, payload)},
            timeout=TRANSFER_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
    except httpx.HTTPError as exc:
        raise IpfsError(f"IPFS_ADD_FAILED: {filename}: {exc}") from exc
    lines = [line.strip() for line in response.text.splitlines() if line.strip()]
    if not lines:
        raise IpfsError(f"IPFS_ADD_FAILED: {filename}: empty response")
    try:
        return str(json.loads(lines[-1])["Hash"])
    except (ValueError, KeyError, TypeError) as exc:
        raise IpfsError(f"IPFS_ADD_FAILED: {filename}: unexpected response {lines[-1]!r}") from exc


def cat_bytes(url: str, cid: str, *, max_bytes: int, client: httpx.Client | None = None) -> bytes:
    """Read one artifact, refusing any that exceeds `max_bytes` before it is fully read."""

    if max_bytes <= 0:
        raise IpfsError("max_bytes must be a positive integer")
    chunks: list[bytes] = []
    total = 0
    try:
        with _stream(client, f"{url}/api/v0/cat", params={"arg": cid}, timeout=TRANSFER_TIMEOUT_SECONDS) as response:
            response.raise_for_status()
            declared = response.headers.get("content-length")
            if declared is not None and declared.isdigit() and int(declared) > max_bytes:
                raise IpfsError(_too_large(cid, int(declared), max_bytes))
            for chunk in response.iter_bytes():
                total += len(chunk)
                if total > max_bytes:
                    raise IpfsError(_too_large(cid, total, max_bytes))
                chunks.append(chunk)
    except httpx.HTTPError as exc:
        raise IpfsError(f"IPFS_CAT_FAILED: {cid}: {exc}") from exc
    return b"".join(chunks)


def cat_json(url: str, cid: str, *, max_bytes: int, client: httpx.Client | None = None) -> Any:
    raw = cat_bytes(url, cid, max_bytes=max_bytes, client=client)
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise IpfsError(f"IPFS_ARTIFACT_NOT_JSON: {cid}: {exc}") from exc


def _too_large(cid: str, size: int, max_bytes: int) -> str:
    return (
        f"IPFS_ARTIFACT_TOO_LARGE: {cid} is over {max_bytes} bytes (read {size}); "
        f"raise {IPFS_MAX_BYTES_ENV} to read it"
    )


def _post(client: httpx.Client | None, url: str, **kwargs: Any) -> httpx.Response:
    return client.post(url, **kwargs) if client is not None else httpx.post(url, **kwargs)


def _stream(client: httpx.Client | None, url: str, **kwargs: Any):
    return client.stream("POST", url, **kwargs) if client is not None else httpx.stream("POST", url, **kwargs)
