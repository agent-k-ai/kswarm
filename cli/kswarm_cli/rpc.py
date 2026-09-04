from __future__ import annotations

import base64
import re
import time
from dataclasses import dataclass
from typing import Any

import httpx
from solders.hash import Hash
from solders.keypair import Keypair
from solders.transaction import Transaction

from kswarm_cli.constants import PROTOCOL_ERRORS


# `getMultipleAccounts` accepts at most 100 pubkeys per call.
MULTIPLE_ACCOUNTS_CHUNK = 100


class RpcError(RuntimeError):
    def __init__(self, code: str, message: str, payload: Any | None = None) -> None:
        self.code = code
        self.payload = payload
        super().__init__(message)


@dataclass
class RpcClient:
    url: str
    commitment: str = "confirmed"
    timeout_seconds: float = 30.0

    def __post_init__(self) -> None:
        self._client = httpx.Client(timeout=self.timeout_seconds)
        self._request_id = 0

    def request(self, method: str, params: list[Any] | None = None) -> Any:
        self._request_id += 1
        response = self._client.post(
            self.url,
            json={
                "jsonrpc": "2.0",
                "id": self._request_id,
                "method": method,
                "params": params or [],
            },
        )
        response.raise_for_status()
        payload = response.json()
        if payload.get("error"):
            error = payload["error"]
            message = error.get("message", "RPC error")
            raise RpcError(_structured_error_code(message, error), message, error)
        return payload.get("result")

    def get_latest_blockhash(self) -> Hash:
        result = self.request("getLatestBlockhash", [{"commitment": self.commitment}])
        return Hash.from_string(result["value"]["blockhash"])

    def send_transaction(self, transaction: Transaction) -> str:
        raw = base64.b64encode(bytes(transaction)).decode("ascii")
        result = self.request(
            "sendTransaction",
            [
                raw,
                {
                    "encoding": "base64",
                    "skipPreflight": False,
                    "preflightCommitment": self.commitment,
                    "maxRetries": 5,
                },
            ],
        )
        self.confirm_signature(result)
        return str(result)

    def confirm_signature(self, signature: str, timeout_seconds: float = 60.0) -> None:
        deadline = time.time() + timeout_seconds
        while time.time() < deadline:
            statuses = self.request("getSignatureStatuses", [[signature], {"searchTransactionHistory": True}])
            value = statuses["value"][0]
            if value:
                if value.get("err"):
                    raise RpcError("TransactionFailed", f"transaction failed: {value['err']}", value)
                status = value.get("confirmationStatus")
                if self.commitment == "processed" or status in {"confirmed", "finalized"}:
                    return
            time.sleep(0.5)
        raise RpcError("ConfirmationTimeout", f"timed out waiting for {signature}")

    def request_airdrop(self, pubkey: str, lamports: int) -> str:
        signature = self.request("requestAirdrop", [pubkey, lamports, {"commitment": self.commitment}])
        self.confirm_signature(signature)
        return str(signature)

    def get_balance(self, pubkey: str) -> int:
        return int(self.request("getBalance", [pubkey, {"commitment": self.commitment}])["value"])

    def get_account_info(self, pubkey: str) -> dict[str, Any] | None:
        result = self.request(
            "getAccountInfo",
            [pubkey, {"encoding": "base64", "commitment": self.commitment}],
        )
        return result["value"]

    def get_account_info_parsed(self, pubkey: str) -> dict[str, Any] | None:
        """`getAccountInfo` with `jsonParsed` encoding (used for mint metadata)."""
        result = self.request(
            "getAccountInfo",
            [pubkey, {"encoding": "jsonParsed", "commitment": self.commitment}],
        )
        return result["value"]

    def get_multiple_account_infos(self, pubkeys: list[str]) -> list[dict[str, Any] | None]:
        """`getMultipleAccounts` in RPC-limit-sized chunks; `None` for accounts that do not exist."""

        out: list[dict[str, Any] | None] = []
        for start in range(0, len(pubkeys), MULTIPLE_ACCOUNTS_CHUNK):
            chunk = pubkeys[start : start + MULTIPLE_ACCOUNTS_CHUNK]
            result = self.request(
                "getMultipleAccounts",
                [chunk, {"encoding": "base64", "commitment": self.commitment}],
            )
            values = result["value"]
            if len(values) != len(chunk):
                raise RpcError("MalformedResponse", f"getMultipleAccounts returned {len(values)} of {len(chunk)} accounts")
            out.extend(values)
        return out

    def get_account_data(self, pubkey: str) -> bytes | None:
        account = self.get_account_info(pubkey)
        if not account:
            return None
        return base64.b64decode(account["data"][0])

    def account_exists(self, pubkey: str) -> bool:
        return self.get_account_info(pubkey) is not None

    def get_program_accounts(self, program_id: str) -> list[dict[str, Any]]:
        return self.request(
            "getProgramAccounts",
            [program_id, {"encoding": "base64", "commitment": self.commitment}],
        )

    def minimum_balance_for_rent_exemption(self, size: int) -> int:
        return int(self.request("getMinimumBalanceForRentExemption", [size]))

    def get_token_account_balance(self, ata: str) -> dict[str, Any] | None:
        account = self.get_account_info(ata)
        if not account:
            return None
        return self.request("getTokenAccountBalance", [ata, {"commitment": self.commitment}])["value"]

    def get_signatures_for_address(self, address: str, limit: int = 25) -> list[dict[str, Any]]:
        return self.request(
            "getSignaturesForAddress",
            [address, {"limit": limit, "commitment": self.commitment}],
        )

    def get_transaction(self, signature: str) -> dict[str, Any] | None:
        return self.request(
            "getTransaction",
            [
                signature,
                {
                    "encoding": "json",
                    "commitment": self.commitment,
                    "maxSupportedTransactionVersion": 0,
                },
            ],
        )


def sign_and_send(
    rpc: RpcClient,
    payer: Keypair,
    instructions: list[Any],
    extra_signers: list[Keypair] | None = None,
) -> str:
    blockhash = rpc.get_latest_blockhash()
    signers = [payer]
    for signer in extra_signers or []:
        if str(signer.pubkey()) != str(payer.pubkey()):
            signers.append(signer)
    tx = Transaction.new_signed_with_payer(instructions, payer.pubkey(), signers, blockhash)
    return rpc.send_transaction(tx)


def _structured_error_code(message: str, payload: dict[str, Any]) -> str:
    text = message
    logs = (((payload.get("data") or {}).get("logs")) or [])
    if logs:
        text = f"{text}\n" + "\n".join(str(log) for log in logs)
    match = re.search(r"custom program error: 0x([0-9a-fA-F]+)", text)
    if match:
        value = int(match.group(1), 16)
        anchor_index = value - 6000
        if 0 <= anchor_index < len(PROTOCOL_ERRORS):
            return PROTOCOL_ERRORS[anchor_index]
        return f"CustomProgramError({match.group(1)})"
    return str(payload.get("code", "RpcError"))
