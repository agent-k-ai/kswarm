from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

from solders.pubkey import Pubkey

from .cli_shim import (
    NamedWallet,
    ProtocolAddresses,
    RpcClient,
    challenge_job_ix,
    claim_job_ix,
    fetch_all_jobs,
    fetch_config,
    fetch_job,
    fetch_worker,
    load_keypair_file,
    load_wallet,
    sign_and_send,
    submit_receipt_ix,
    submit_verifier_attestation_ix,
)


def load_session_wallet(keypair_name: str, wallet_file: Path | None) -> NamedWallet:
    """The signing wallet: the mounted key file when given, else the named CLI wallet."""

    if wallet_file is not None:
        return NamedWallet(name=wallet_file.stem, path=wallet_file, keypair=load_keypair_file(wallet_file))
    return load_wallet(keypair_name)


@dataclass
class ProtocolSession:
    rpc_url: str
    keypair_name: str
    program_id: Pubkey
    wallet_file: Path | None = None
    commitment: str = "confirmed"

    def __post_init__(self) -> None:
        self.rpc = RpcClient(self.rpc_url, self.commitment)
        self.wallet = load_session_wallet(self.keypair_name, self.wallet_file)
        config = fetch_config(self.rpc, self.program_id)
        if not config:
            raise RuntimeError("protocol config not initialized")
        # Payment mint and token program come from the on-chain config, never from a constant.
        self.proto: ProtocolAddresses = config.addresses(self.program_id)
        self.payment_decimals = config.payment_decimals
        self.worker = self.proto.worker_pda(self.wallet.pubkey)

    def jobs(self):
        return fetch_all_jobs(self.rpc, self.program_id)

    def job(self, job_key: Pubkey):
        return fetch_job(self.rpc, job_key)

    def worker_account(self):
        """The on-chain Worker account for this wallet, or None when not registered."""

        return fetch_worker(self.rpc, self.worker)

    def claim_job(self, job_key: Pubkey) -> str:
        return sign_and_send(
            self.rpc,
            self.wallet.keypair,
            [claim_job_ix(self.proto, self.wallet.pubkey, job_key)],
        )

    def submit_receipt(self, job_key: Pubkey, output_cid: str, result_bytes: bytes) -> str:
        return sign_and_send(
            self.rpc,
            self.wallet.keypair,
            [submit_receipt_ix(self.program_id, self.wallet.pubkey, job_key, output_cid, result_bytes)],
        )

    def submit_attestation(self, job_key: Pubkey, result_bytes: bytes, evidence_cid: str, software_digest: bytes) -> str:
        result_hash = hashlib.sha256(result_bytes).digest()
        return sign_and_send(
            self.rpc,
            self.wallet.keypair,
            [
                submit_verifier_attestation_ix(
                    self.proto,
                    self.wallet.pubkey,
                    job_key,
                    result_hash,
                    evidence_cid,
                    software_digest,
                )
            ],
        )

    def challenge_job(self, job_key: Pubkey, job_account) -> str:
        return sign_and_send(
            self.rpc,
            self.wallet.keypair,
            [challenge_job_ix(self.proto, self.wallet.pubkey, job_key, job_account)],
        )
