#!/usr/bin/env python3
"""The proving service a containerized aggregator asks for a Bonsol proof.

The aggregator image carries no Bonsol CLI, no docker socket and no Bonsol client
keypair, and it should not: those are the operator's proving infrastructure, not part
of a hardened worker. This service holds them and runs
`protocol/scripts/bonsol-aggregate-hook.py` on request.

    protocol/scripts/bonsol-hook-server.py --bind 0.0.0.0 --port 38099

It is a thin wrapper on purpose. It forwards the request body to the hook verbatim and
returns the hook's stdout verbatim, so the aggregator's own checks -- every digest
against its own reduction, and every digest against the job account -- are what decide
whether an execution is accepted. A service that proved a different claim is refused by
the caller, not trusted by it.

Nothing here authenticates the caller. Bind it to a network only the swarm can reach.
It spends the Bonsol client keypair's SOL on marker rent, image deploys and execution
requests, so an open port is a way to drain that key, not a way to forge a proof.

It also serves the guest input. A Solana transaction carries at most 1232 raw bytes and
an aggregate artifact does not fit, so the execution request names a URL the Bonsol node
fetches. `POST /input` stores framed bytes under their SHA-256 and returns that URL;
`GET /input/<sha256>` returns them unchanged. Nothing is trusted here either: the Bonsol
program compares the digest the guest committed with the `inputHash` on the request, so
a store that returned different bytes yields no marker rather than a wrong one.

Environment: everything `bonsol-aggregate-hook.py` reads is inherited, so one place
configures the RPC, the CLIs, the runtime directory and the input store. The hook is
given `KSWARM_BONSOL_INPUT_PUBLISH_URL` pointing back at this service unless the caller
set it, so the default composition needs no external storage at all.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import subprocess
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
HOOK = REPO_ROOT / "protocol" / "scripts" / "bonsol-aggregate-hook.py"
MAX_REQUEST_BYTES = 4 * 1024 * 1024
LOGGER = logging.getLogger("kswarm.bonsol_hook_server")

# Framed guest inputs this process has been asked to serve, keyed by SHA-256. They are
# small (a few kilobytes) and live only as long as the service, which is exactly as long
# as the executions that reference them.
INPUTS: dict[str, bytes] = {}


class Handler(BaseHTTPRequestHandler):
    hook_timeout_seconds = 1800.0
    python_bin = sys.executable
    # The base URL the Bonsol node uses to reach this service. It is not the bind
    # address: the node is in a container and this process is on the host.
    public_base = "http://127.0.0.1:38099"

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler's naming
        path = self.path.rstrip("/")
        if path not in {"", "/prove", "/input"}:
            self._reply(404, {"error": f"no such path: {self.path}"})
            return
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > MAX_REQUEST_BYTES:
            self._reply(413, {"error": f"body must be 1..{MAX_REQUEST_BYTES} bytes; got {length}"})
            return
        raw = self.rfile.read(length)
        if path == "/input":
            digest = hashlib.sha256(raw).hexdigest()
            INPUTS[digest] = raw
            LOGGER.info("stored a %d-byte guest input as %s", len(raw), digest)
            self._reply(200, {"url": f"{self.public_base}/input/{digest}", "sha256": digest})
            return
        payload = raw.decode("utf-8", errors="replace")
        try:
            job = json.loads(payload).get("aggregate_job", "?")
        except json.JSONDecodeError:
            self._reply(400, {"error": "body is not JSON"})
            return

        LOGGER.info("proving aggregate job=%s", job)
        completed = subprocess.run(
            [self.python_bin, str(HOOK), payload],
            cwd=str(REPO_ROOT),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=self.hook_timeout_seconds,
        )
        if completed.returncode != 0:
            LOGGER.error("hook exited %d for job=%s: %s", completed.returncode, job, completed.stderr.strip()[-2000:])
            self._reply(502, {"error": completed.stderr.strip()[-2000:] or f"hook exited {completed.returncode}"})
            return
        try:
            binding = json.loads(completed.stdout)
        except json.JSONDecodeError:
            self._reply(502, {"error": f"hook did not print JSON: {completed.stdout[:500]!r}"})
            return
        LOGGER.info("proved aggregate job=%s execution_id=%s", job, binding.get("execution_id"))
        self._reply(200, binding)

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler's naming
        path = self.path.rstrip("/")
        if path == "/healthz":
            self._reply(200, {"ok": True, "hook": str(HOOK), "hook_present": HOOK.is_file(), "inputs": len(INPUTS)})
            return
        if path.startswith("/input/"):
            body = INPUTS.get(path[len("/input/"):])
            if body is None:
                self._reply(404, {"error": "no such input"})
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self._reply(404, {"error": f"no such path: {self.path}"})

    def _reply(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args) -> None:
        LOGGER.info("%s - %s", self.address_string(), fmt % args)


def main() -> int:
    parser = argparse.ArgumentParser(description="Serve Bonsol aggregate proof requests.")
    parser.add_argument("--bind", default=os.environ.get("KSWARM_BONSOL_HOOK_BIND", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("KSWARM_BONSOL_HOOK_PORT", "38099")))
    parser.add_argument("--timeout", type=float, default=float(os.environ.get("KSWARM_BONSOL_HOOK_TIMEOUT_SECONDS", "1800")))
    parser.add_argument(
        "--public-base",
        default=os.environ.get("KSWARM_BONSOL_HOOK_PUBLIC_BASE", ""),
        help="Base URL the Bonsol node reaches this service on. Defaults to the bind address.",
    )
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    if not HOOK.is_file():
        LOGGER.error("no hook at %s", HOOK)
        return 1
    Handler.hook_timeout_seconds = args.timeout
    Handler.public_base = (args.public_base or f"http://{args.bind}:{args.port}").rstrip("/")
    # The hook publishes large guest inputs back to this service unless told otherwise,
    # so the default composition needs no other storage.
    os.environ.setdefault("KSWARM_BONSOL_INPUT_PUBLISH_URL", f"{Handler.public_base}/input")
    server = ThreadingHTTPServer((args.bind, args.port), Handler)
    LOGGER.info("bonsol hook server on %s:%d, hook %s", args.bind, args.port, HOOK)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        LOGGER.info("stopping")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
