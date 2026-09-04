"""The branch receipt helpers, and the hook a containerized aggregator calls.

Both are thin by design, and both sit where a mistake would be silent: a pin that
quietly reads as "no pin" would let a verifier accept a receipt from any guest, and a
hook that quietly rewrote a digest would ask a proving service for a different claim.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from aggregator_runner import bonsol_http_hook
from worker_common import branch_receipt


def test_the_pin_comes_from_the_environment_first(tmp_path: Path) -> None:
    recorded = tmp_path / "image-id"
    recorded.write_text("bb" * 32 + "\n", encoding="utf-8")
    environ = {
        branch_receipt.ZKVM_IMAGE_ID_ENV: "AA" * 32,
        branch_receipt.ZKVM_IMAGE_ID_FILE_ENV: str(recorded),
    }
    assert branch_receipt.pinned_image_id(environ) == "aa" * 32


def test_the_pin_falls_back_to_the_file_the_image_ships(tmp_path: Path) -> None:
    recorded = tmp_path / "image-id"
    recorded.write_text("  " + "cd" * 32 + "\n", encoding="utf-8")
    assert branch_receipt.pinned_image_id({branch_receipt.ZKVM_IMAGE_ID_FILE_ENV: str(recorded)}) == "cd" * 32


@pytest.mark.parametrize("contents", ["", "not hex", "ab" * 31, "zz" * 32])
def test_a_file_that_is_not_an_image_id_pins_nothing_rather_than_crashing(tmp_path: Path, contents: str) -> None:
    recorded = tmp_path / "image-id"
    recorded.write_text(contents, encoding="utf-8")
    assert branch_receipt.pinned_image_id({branch_receipt.ZKVM_IMAGE_ID_FILE_ENV: str(recorded)}) is None


def test_a_missing_file_pins_nothing(tmp_path: Path) -> None:
    assert branch_receipt.pinned_image_id({branch_receipt.ZKVM_IMAGE_ID_FILE_ENV: str(tmp_path / "absent")}) is None
    assert branch_receipt.pinned_image_id({}) is None


def _serve(tmp_path: Path, body: str, status: int = 200) -> tuple[str, Path]:
    """A one-request HTTP server that records what it was sent."""

    seen = tmp_path / "seen.json"
    script = tmp_path / "server.py"
    script.write_text(
        "import http.server, sys, pathlib\n"
        "SEEN = pathlib.Path(sys.argv[1])\n"
        "BODY = sys.argv[2].encode()\n"
        "STATUS = int(sys.argv[3])\n"
        "class H(http.server.BaseHTTPRequestHandler):\n"
        "    def do_POST(self):\n"
        "        SEEN.write_bytes(self.rfile.read(int(self.headers['Content-Length'])))\n"
        "        self.send_response(STATUS)\n"
        "        self.send_header('Content-Length', str(len(BODY)))\n"
        "        self.end_headers()\n"
        "        self.wfile.write(BODY)\n"
        "    def log_message(self, *a):\n"
        "        pass\n"
        "server = http.server.HTTPServer(('127.0.0.1', 0), H)\n"
        "print(server.server_address[1], flush=True)\n"
        "server.handle_request()\n",
        encoding="utf-8",
    )
    process = subprocess.Popen([sys.executable, str(script), str(seen), body, str(status)], stdout=subprocess.PIPE, text=True)
    port = process.stdout.readline().strip()
    return f"http://127.0.0.1:{port}/prove", seen


def test_the_hook_forwards_the_payload_verbatim_and_returns_the_answer(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
    answer = {"execution_id": "exec-1", "image_id": "11" * 32}
    url, seen = _serve(tmp_path, json.dumps(answer))
    monkeypatch.setenv(bonsol_http_hook.HOOK_URL_ENV, url)
    payload = json.dumps({"aggregate_job": "Job", "input_digest": "22" * 32}, sort_keys=True)

    assert bonsol_http_hook.main(["hook", payload]) == 0
    # Byte-identical: rewriting a digest here would ask for a different claim.
    assert seen.read_text(encoding="utf-8") == payload
    assert json.loads(capsys.readouterr().out) == answer


def test_the_hook_fails_when_it_has_no_service_to_call(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(bonsol_http_hook.HOOK_URL_ENV, raising=False)
    assert bonsol_http_hook.main(["hook", "{}"]) == 1


def test_the_hook_fails_on_an_error_a_bad_body_or_no_payload(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    url, _ = _serve(tmp_path, "boom", status=502)
    monkeypatch.setenv(bonsol_http_hook.HOOK_URL_ENV, url)
    assert bonsol_http_hook.main(["hook", "{}"]) == 1

    url, _ = _serve(tmp_path, "not json")
    monkeypatch.setenv(bonsol_http_hook.HOOK_URL_ENV, url)
    assert bonsol_http_hook.main(["hook", "{}"]) == 1

    url, _ = _serve(tmp_path, "[]")
    monkeypatch.setenv(bonsol_http_hook.HOOK_URL_ENV, url)
    assert bonsol_http_hook.main(["hook", "{}"]) == 1

    monkeypatch.setenv(bonsol_http_hook.HOOK_URL_ENV, "http://127.0.0.1:1/prove")
    assert bonsol_http_hook.main(["hook", "{}"]) == 1
    assert bonsol_http_hook.main(["hook"]) == 2
