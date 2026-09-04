from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from solders.instruction import AccountMeta, Instruction
from solders.keypair import Keypair
from solders.pubkey import Pubkey
from solders.system_program import CreateAccountParams, ID as SYSTEM_PROGRAM_ID, create_account

from kswarm_cli.constants import (
    ASSOCIATED_TOKEN_PROGRAM_ID,
    KNOWN_TOKEN_PROGRAMS,
    TOKEN_2022_PROGRAM_ID,
    TOKEN_PROGRAM_ID,
)
from kswarm_cli.encoding import u8, u64
from kswarm_cli.rpc import RpcClient, sign_and_send


# Both token programs share the legacy 82-byte mint layout when no extensions are used.
MINT_ACCOUNT_SIZE = 82


@dataclass(frozen=True)
class MintInfo:
    """On-chain facts about a mint that every token instruction depends on."""

    mint: Pubkey
    token_program: Pubkey
    decimals: int


def validate_token_program(token_program: Pubkey) -> Pubkey:
    if token_program not in KNOWN_TOKEN_PROGRAMS:
        raise ValueError(
            f"unsupported token program {token_program}; expected {TOKEN_PROGRAM_ID} (SPL Token) "
            f"or {TOKEN_2022_PROGRAM_ID} (Token-2022)"
        )
    return token_program


def associated_token_address(mint: Pubkey, owner: Pubkey, token_program: Pubkey) -> Pubkey:
    """Derive the ATA for `owner` under the token program that owns `mint`.

    Seeds: [owner, token_program, mint] under the Associated Token program. The token
    program is part of the seed, so an ATA derived with the wrong program is a
    different (and empty) address.
    """
    validate_token_program(token_program)
    return Pubkey.find_program_address(
        [bytes(owner), bytes(token_program), bytes(mint)],
        ASSOCIATED_TOKEN_PROGRAM_ID,
    )[0]


def create_associated_token_account_idempotent_ix(
    payer: Pubkey, mint: Pubkey, owner: Pubkey, token_program: Pubkey
) -> Instruction:
    ata = associated_token_address(mint, owner, token_program)
    return Instruction(
        ASSOCIATED_TOKEN_PROGRAM_ID,
        bytes([1]),
        [
            AccountMeta(payer, True, True),
            AccountMeta(ata, False, True),
            AccountMeta(owner, False, False),
            AccountMeta(mint, False, False),
            AccountMeta(SYSTEM_PROGRAM_ID, False, False),
            AccountMeta(token_program, False, False),
        ],
    )


def initialize_mint2_ix(mint: Pubkey, decimals: int, authority: Pubkey, token_program: Pubkey) -> Instruction:
    validate_token_program(token_program)
    data = bytes([20]) + u8(decimals) + bytes(authority) + bytes(4)
    return Instruction(token_program, data, [AccountMeta(mint, False, True)])


def mint_to_checked_ix(
    mint: Pubkey,
    destination: Pubkey,
    authority: Pubkey,
    amount: int,
    decimals: int,
    token_program: Pubkey,
) -> Instruction:
    validate_token_program(token_program)
    return Instruction(
        token_program,
        bytes([14]) + u64(amount) + u8(decimals),
        [
            AccountMeta(mint, False, True),
            AccountMeta(destination, False, True),
            AccountMeta(authority, True, False),
        ],
    )


def transfer_checked_ix(
    source: Pubkey,
    mint: Pubkey,
    destination: Pubkey,
    authority: Pubkey,
    amount: int,
    decimals: int,
    token_program: Pubkey,
) -> Instruction:
    validate_token_program(token_program)
    return Instruction(
        token_program,
        bytes([12]) + u64(amount) + u8(decimals),
        [
            AccountMeta(source, False, True),
            AccountMeta(mint, False, False),
            AccountMeta(destination, False, True),
            AccountMeta(authority, True, False),
        ],
    )


def create_mint_instructions(
    payer: Pubkey,
    mint: Pubkey,
    authority: Pubkey,
    decimals: int,
    lamports: int,
    token_program: Pubkey,
) -> list[Instruction]:
    validate_token_program(token_program)
    return [
        create_account(
            CreateAccountParams(
                from_pubkey=payer,
                to_pubkey=mint,
                lamports=lamports,
                space=MINT_ACCOUNT_SIZE,
                owner=token_program,
            )
        ),
        initialize_mint2_ix(mint, decimals, authority, token_program),
    ]


def create_mint(
    rpc: RpcClient,
    payer: Keypair,
    authority: Pubkey,
    decimals: int,
    token_program: Pubkey,
) -> tuple[Pubkey, str]:
    mint = Keypair()
    lamports = rpc.minimum_balance_for_rent_exemption(MINT_ACCOUNT_SIZE)
    instructions = create_mint_instructions(payer.pubkey(), mint.pubkey(), authority, decimals, lamports, token_program)
    signature = sign_and_send(rpc, payer, instructions, [mint])
    return mint.pubkey(), signature


def ensure_ata_ix_if_missing(
    rpc: RpcClient, payer: Pubkey, mint: Pubkey, owner: Pubkey, token_program: Pubkey
) -> tuple[Pubkey, Instruction | None]:
    ata = associated_token_address(mint, owner, token_program)
    if rpc.account_exists(str(ata)):
        return ata, None
    return ata, create_associated_token_account_idempotent_ix(payer, mint, owner, token_program)


def parse_mint_account(mint: Pubkey, account: dict[str, Any] | None) -> MintInfo:
    """Turn a jsonParsed `getAccountInfo` value into `MintInfo`, or raise `ValueError`."""
    if account is None:
        raise ValueError(f"mint account not found: {mint}")
    owner = Pubkey.from_string(str(account.get("owner", "")))
    if owner not in KNOWN_TOKEN_PROGRAMS:
        raise ValueError(f"{mint} is owned by {owner}, which is not a token program")
    parsed = (account.get("data") or {}).get("parsed") if isinstance(account.get("data"), dict) else None
    if not isinstance(parsed, dict) or parsed.get("type") != "mint":
        raise ValueError(f"{mint} is not a mint account")
    decimals = (parsed.get("info") or {}).get("decimals")
    if not isinstance(decimals, int) or isinstance(decimals, bool) or not 0 <= decimals <= 255:
        raise ValueError(f"{mint} has no valid decimals field")
    return MintInfo(mint=mint, token_program=owner, decimals=decimals)


def fetch_mint_info(rpc: RpcClient, mint: Pubkey) -> MintInfo:
    """Read the mint's owner program and decimals from chain."""
    return parse_mint_account(mint, rpc.get_account_info_parsed(str(mint)))
