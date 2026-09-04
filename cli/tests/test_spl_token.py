from __future__ import annotations

import pytest
from solders.keypair import Keypair
from solders.pubkey import Pubkey
from solders.system_program import ID as SYSTEM_PROGRAM_ID
from spl.token.instructions import get_associated_token_address as solana_py_ata

from kswarm_cli.constants import (
    ASSOCIATED_TOKEN_PROGRAM_ID,
    KAI_MAINNET_MINT,
    TOKEN_2022_PROGRAM_ID,
    TOKEN_PROGRAM_ID,
)
from kswarm_cli.spl_token import (
    MINT_ACCOUNT_SIZE,
    MintInfo,
    associated_token_address,
    create_associated_token_account_idempotent_ix,
    create_mint_instructions,
    initialize_mint2_ix,
    mint_to_checked_ix,
    parse_mint_account,
    transfer_checked_ix,
    validate_token_program,
)


OWNER = Pubkey.from_string("9xQeWvG816bUx9EPjHmaT23yvVM2ZWbrrpZb9PusVFin")
MINT = Pubkey.from_string("4k3Dyjzvzp8eMZWUXbBCjEvwSkkk59S5iCNLY3QrkX6R")


def documented_seed_ata(owner: Pubkey, mint: Pubkey, token_program: Pubkey) -> Pubkey:
    """The ATA seeds from the Associated Token Account program spec."""
    return Pubkey.find_program_address(
        [bytes(owner), bytes(token_program), bytes(mint)],
        ASSOCIATED_TOKEN_PROGRAM_ID,
    )[0]


@pytest.mark.parametrize("token_program", [TOKEN_PROGRAM_ID, TOKEN_2022_PROGRAM_ID])
def test_ata_matches_documented_seeds(token_program: Pubkey) -> None:
    assert associated_token_address(MINT, OWNER, token_program) == documented_seed_ata(OWNER, MINT, token_program)


@pytest.mark.parametrize("token_program", [TOKEN_PROGRAM_ID, TOKEN_2022_PROGRAM_ID])
def test_ata_matches_solana_py_oracle(token_program: Pubkey) -> None:
    assert associated_token_address(MINT, OWNER, token_program) == solana_py_ata(OWNER, MINT, token_program)


def test_ata_differs_between_token_programs() -> None:
    assert associated_token_address(MINT, OWNER, TOKEN_PROGRAM_ID) != associated_token_address(
        MINT, OWNER, TOKEN_2022_PROGRAM_ID
    )


def test_ata_for_off_curve_owner_matches_oracle() -> None:
    pda = Pubkey.find_program_address([b"job"], TOKEN_PROGRAM_ID)[0]
    assert associated_token_address(KAI_MAINNET_MINT, pda, TOKEN_PROGRAM_ID) == solana_py_ata(
        pda, KAI_MAINNET_MINT, TOKEN_PROGRAM_ID
    )


def test_kai_mainnet_ata_for_known_owner() -> None:
    # Vector: the KAI mint (classic SPL Token, 6 decimals) and a fixed owner.
    # Independent oracle: solana-py's ATA derivation.
    ata = associated_token_address(KAI_MAINNET_MINT, OWNER, TOKEN_PROGRAM_ID)
    assert ata == solana_py_ata(OWNER, KAI_MAINNET_MINT, TOKEN_PROGRAM_ID)
    assert ata == documented_seed_ata(OWNER, KAI_MAINNET_MINT, TOKEN_PROGRAM_ID)


def test_ata_rejects_unknown_token_program() -> None:
    with pytest.raises(ValueError, match="unsupported token program"):
        associated_token_address(MINT, OWNER, SYSTEM_PROGRAM_ID)


def test_validate_token_program_accepts_both_known_programs() -> None:
    assert validate_token_program(TOKEN_PROGRAM_ID) == TOKEN_PROGRAM_ID
    assert validate_token_program(TOKEN_2022_PROGRAM_ID) == TOKEN_2022_PROGRAM_ID
    with pytest.raises(ValueError):
        validate_token_program(ASSOCIATED_TOKEN_PROGRAM_ID)


@pytest.mark.parametrize("token_program", [TOKEN_PROGRAM_ID, TOKEN_2022_PROGRAM_ID])
def test_create_ata_ix_targets_the_given_token_program(token_program: Pubkey) -> None:
    payer = Keypair().pubkey()
    ix = create_associated_token_account_idempotent_ix(payer, MINT, OWNER, token_program)
    assert ix.program_id == ASSOCIATED_TOKEN_PROGRAM_ID
    assert ix.data == bytes([1])
    keys = [meta.pubkey for meta in ix.accounts]
    assert keys == [payer, associated_token_address(MINT, OWNER, token_program), OWNER, MINT, SYSTEM_PROGRAM_ID, token_program]
    assert ix.accounts[0].is_signer and ix.accounts[0].is_writable
    assert ix.accounts[1].is_writable and not ix.accounts[1].is_signer


@pytest.mark.parametrize("token_program", [TOKEN_PROGRAM_ID, TOKEN_2022_PROGRAM_ID])
def test_initialize_mint2_ix_layout(token_program: Pubkey) -> None:
    authority = Keypair().pubkey()
    ix = initialize_mint2_ix(MINT, 6, authority, token_program)
    assert ix.program_id == token_program
    assert ix.data == bytes([20, 6]) + bytes(authority) + bytes(4)
    assert len(ix.data) == 38
    assert [meta.pubkey for meta in ix.accounts] == [MINT]


def test_mint_to_checked_ix_layout() -> None:
    authority = Keypair().pubkey()
    destination = Keypair().pubkey()
    ix = mint_to_checked_ix(MINT, destination, authority, 50_000_000_000, 6, TOKEN_PROGRAM_ID)
    assert ix.program_id == TOKEN_PROGRAM_ID
    assert ix.data == bytes([14]) + (50_000_000_000).to_bytes(8, "little") + bytes([6])
    assert [meta.pubkey for meta in ix.accounts] == [MINT, destination, authority]
    assert ix.accounts[2].is_signer


def test_transfer_checked_ix_layout() -> None:
    source, destination, authority = (Keypair().pubkey() for _ in range(3))
    ix = transfer_checked_ix(source, MINT, destination, authority, 1_000_000, 6, TOKEN_2022_PROGRAM_ID)
    assert ix.program_id == TOKEN_2022_PROGRAM_ID
    assert ix.data == bytes([12]) + (1_000_000).to_bytes(8, "little") + bytes([6])
    assert [meta.pubkey for meta in ix.accounts] == [source, MINT, destination, authority]


def test_create_mint_instructions_assign_the_token_program_as_owner() -> None:
    payer, mint, authority = (Keypair().pubkey() for _ in range(3))
    instructions = create_mint_instructions(payer, mint, authority, 6, 1_461_600, TOKEN_PROGRAM_ID)
    assert len(instructions) == 2
    create, init = instructions
    assert create.program_id == SYSTEM_PROGRAM_ID
    assert create.data[-32:] == bytes(TOKEN_PROGRAM_ID)
    assert int.from_bytes(create.data[12:20], "little") == MINT_ACCOUNT_SIZE
    assert init.program_id == TOKEN_PROGRAM_ID


def _mint_account(owner: Pubkey, decimals: int = 6) -> dict:
    return {
        "owner": str(owner),
        "lamports": 1_461_600,
        "data": {"program": "spl-token", "parsed": {"type": "mint", "info": {"decimals": decimals, "supply": "0"}}},
    }


def test_parse_mint_account_reads_classic_mint() -> None:
    info = parse_mint_account(KAI_MAINNET_MINT, _mint_account(TOKEN_PROGRAM_ID, 6))
    assert info == MintInfo(mint=KAI_MAINNET_MINT, token_program=TOKEN_PROGRAM_ID, decimals=6)


def test_parse_mint_account_reads_token_2022_mint() -> None:
    info = parse_mint_account(MINT, _mint_account(TOKEN_2022_PROGRAM_ID, 9))
    assert info.token_program == TOKEN_2022_PROGRAM_ID
    assert info.decimals == 9


def test_parse_mint_account_rejects_missing_account() -> None:
    with pytest.raises(ValueError, match="not found"):
        parse_mint_account(MINT, None)


def test_parse_mint_account_rejects_non_token_owner() -> None:
    with pytest.raises(ValueError, match="not a token program"):
        parse_mint_account(MINT, _mint_account(SYSTEM_PROGRAM_ID))


def test_parse_mint_account_rejects_token_account() -> None:
    account = _mint_account(TOKEN_PROGRAM_ID)
    account["data"]["parsed"]["type"] = "account"
    with pytest.raises(ValueError, match="not a mint"):
        parse_mint_account(MINT, account)


def test_parse_mint_account_rejects_bad_decimals() -> None:
    account = _mint_account(TOKEN_PROGRAM_ID)
    account["data"]["parsed"]["info"]["decimals"] = "6"
    with pytest.raises(ValueError, match="decimals"):
        parse_mint_account(MINT, account)
