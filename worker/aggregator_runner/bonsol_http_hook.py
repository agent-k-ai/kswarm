"""`KSWARM_BONSOL_AGGREGATE_COMMAND` for a containerized aggregator.

The aggregator image is hardened: no docker socket, no Solana keypair beyond its own
wallet, no Bonsol CLI. Requesting a Bonsol proof needs all three, so the work is done
by a proving service and this module is the client that reaches it.

    KSWARM_BONSOL_AGGREGATE_COMMAND="python -m aggregator_runner.bonsol_http_hook"
    KSWARM_BONSOL_HOOK_URL="http://proving-host:38099/prove"

The contract is unchanged: the runner appends one JSON argument, the command prints one
JSON object on stdout, and the runner then checks every field against its own reduction
and against the job account. This client adds nothing and interprets nothing -- it
forwards the payload and returns the answer, so a proving service that proved a
different claim is caught by the same checks that catch a local hook doing it.

`protocol/scripts/bonsol-hook-server.py` is the service that answers.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request


HOOK_URL_ENV = "KSWARM_BONSOL_HOOK_URL"
HOOK_TIMEOUT_ENV = "KSWARM_BONSOL_HOOK_URL_TIMEOUT_SECONDS"
DEFAULT_TIMEOUT_SECONDS = 1800.0


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print("usage: python -m aggregator_runner.bonsol_http_hook '<payload json>'", file=sys.stderr)
        return 2
    url = os.environ.get(HOOK_URL_ENV, "").strip()
    if not url:
        print(f"{HOOK_URL_ENV} is unset: this hook has no proving service to call", file=sys.stderr)
        return 1
    timeout = float(os.environ.get(HOOK_TIMEOUT_ENV, "").strip() or DEFAULT_TIMEOUT_SECONDS)

    # The payload is forwarded verbatim. Parsing it here would only add a second place
    # for the digests to be rewritten.
    request = urllib.request.Request(
        url,
        data=argv[-1].encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8")
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")[:1000]
        print(f"proving service {url} returned HTTP {error.code}: {detail}", file=sys.stderr)
        return 1
    except Exception as error:  # noqa: BLE001 - any failure means no proof was requested
        print(f"proving service {url} did not answer: {error}", file=sys.stderr)
        return 1

    try:
        binding = json.loads(body)
    except json.JSONDecodeError:
        print(f"proving service {url} did not return JSON: {body[:500]!r}", file=sys.stderr)
        return 1
    if not isinstance(binding, dict):
        print(f"proving service {url} returned {type(binding).__name__}, not an object", file=sys.stderr)
        return 1
    print(json.dumps(binding))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
