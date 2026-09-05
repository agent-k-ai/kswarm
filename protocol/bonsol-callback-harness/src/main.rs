use base64::Engine;
use mirofish_bonsol_branch_reducer::{committed_outputs, decode_score_felt, reducer_canonical_bytes};
use risc0_zkvm::sha::{Impl as Risc0Sha256Impl, Sha256 as _};
use serde::{Deserialize, Serialize};
use solana_client::rpc_client::RpcClient;
use solana_client::rpc_config::RpcSendTransactionConfig;
use solana_sdk::{
    bpf_loader_upgradeable,
    commitment_config::CommitmentConfig,
    compute_budget::ComputeBudgetInstruction,
    hash::hash,
    instruction::{AccountMeta, Instruction},
    native_token::LAMPORTS_PER_SOL,
    pubkey::Pubkey,
    signature::{read_keypair_file, Keypair, Signer},
    system_instruction, system_program,
    transaction::Transaction,
};
use spl_associated_token_account::{
    get_associated_token_address_with_program_id, instruction::create_associated_token_account,
};
use spl_token_2022::{
    extension::ExtensionType, instruction as token_2022_instruction, state::Mint,
};
use std::{env, fs, str::FromStr};

const BONSOL_PROGRAM_ID: &str = "BoNsHRcyLLNdtnoDf8hiCNZpyehMC4FDMxs6NTxFi3ew";
const KSWARM_PROGRAM_ID: &str = "ERNzRcYhX6UYboXAAP7vwzbCKsULYu21R4RFNvDD8CkM";
const BONSOL_AGGREGATE_VERIFICATION_LEN: usize = 210;
const AGGREGATE_PROOF_CAPABILITY_HASH: [u8; 32] = [
    0x15, 0xba, 0x06, 0xea, 0xc1, 0x2f, 0x0d, 0xe3, 0x83, 0x4c, 0x5a, 0xec, 0x15, 0x34, 0x37, 0x7d,
    0xa6, 0x74, 0x44, 0x5c, 0x2f, 0x5f, 0xa1, 0xd0, 0xce, 0x69, 0x83, 0x99, 0xe9, 0xe8, 0xd7, 0x89,
];
const ZERO_HASH: [u8; 32] = [0u8; 32];
// The stand-in payment mint copies the KAI layout: classic SPL Token, 6 decimals.
const TOKEN_DECIMALS: u8 = 6;
const UNIT: u64 = 1_000_000;
const REWARD_AMOUNT: u64 = 25 * UNIT;
const REQUIRED_STAKE: u64 = 50 * UNIT;
const CHALLENGE_BOND: u64 = 50 * UNIT;
// The aggregate job requires tier two; stake the default tier-two floor.
const WORKER_STAKE_DEPOSIT: u64 = 250_000 * UNIT;
// Above the default verifier floor (100,000).
const VERIFIER_STAKE_DEPOSIT: u64 = 150_000 * UNIT;
// Default stake floors passed to `initialize_protocol` (owner decision 2026-09-03).
const TIER_ONE_STAKE_FLOOR: u64 = 50_000 * UNIT;
const TIER_TWO_STAKE_FLOOR: u64 = 250_000 * UNIT;
const TIER_THREE_STAKE_FLOOR: u64 = 1_000_000 * UNIT;
const VERIFIER_STAKE_FLOOR: u64 = 100_000 * UNIT;
// `ProtocolConfig.min_challenge_window_seconds`: the smallest challenge window `open_job`
// accepts. This harness drives a local validator and waits out the window in real time, so
// it initializes with the `local` cluster profile's value and opens jobs exactly at it. A
// deployment sizes the floor in `ATTESTATION_WINDOW_SECONDS` rungs instead; see
// `docs/kai-payment-token.md`.
const MIN_CHALLENGE_WINDOW_SECONDS: u32 = 5;
const DEFAULT_INPUT_JSON: &str = "{\"branch_key\":\"baseline\",\"child_job_id\":\"child-baseline-1\",\"parent_request_id\":\"parent-bonsol-eval\",\"line_count\":3,\"word_count\":17,\"score_hex\":\"003a000000000000000000000000000000000000000000000000000000000000\"}";

/// The payment mint and the token program that owns it, as pinned in `ProtocolConfig`.
#[derive(Debug, Clone, Copy)]
struct PaymentMint {
    mint: Pubkey,
    token_program: Pubkey,
}

#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
struct Manifest {
    image_id: String,
}

#[derive(Debug, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
struct PreparedExecution {
    image_id: String,
    image_id_bytes_hex: String,
    execution_id: String,
    input_json: String,
    framed_input_hex: String,
    execution_config_input_hash: String,
    callback_input_digest: String,
    committed_outputs: String,
    committed_outputs_digest: String,
    marker_pda: String,
    expected_marker_pda: String,
    execution_account: Option<String>,
    callback_program_id: String,
    callback_instruction_prefix: Vec<u8>,
    aggregate_job: Option<String>,
    job_escrow_vault: Option<String>,
    payment_mint: Option<String>,
    token_program: Option<String>,
    worker_authority: Option<String>,
    worker: Option<String>,
    worker_payment_account: Option<String>,
    worker_stake_vault: Option<String>,
    verifier_authority: Option<String>,
    verifier: Option<String>,
    verifier_stake_vault: Option<String>,
    challenge_deadline_unix: Option<i64>,
    reward_amount: Option<u64>,
}

#[derive(Debug, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
struct ProductionSetup {
    aggregate_job: String,
    job_escrow_vault: String,
    payment_mint: String,
    token_program: String,
    worker_authority: String,
    worker: String,
    worker_payment_account: String,
    worker_stake_vault: String,
    verifier_authority: String,
    verifier: String,
    verifier_stake_vault: String,
    challenge_deadline_unix: i64,
    reward_amount: u64,
}

fn main() -> Result<(), Box<dyn std::error::Error>> {
    let args: Vec<String> = env::args().collect();
    match args.get(1).map(String::as_str) {
        Some("prepare-production") => prepare_production(&args[2..]),
        Some("verify-production-marker") => verify_production_marker(&args[2..]),
        Some("settle-production") => settle_production(&args[2..]),
        Some("execute-wrong-input-hash") => execute_wrong_input_hash(&args[2..]),
        Some("replay-status") => replay_status(&args[2..]),
        _ => Err("usage: bonsol-callback-harness prepare-production|verify-production-marker|settle-production|execute-wrong-input-hash|replay-status ...".into()),
    }
}

fn prepare_production(args: &[String]) -> Result<(), Box<dyn std::error::Error>> {
    let rpc_url = arg_value(args, "--rpc-url")?;
    let keypair_path = arg_value(args, "--keypair")?;
    let manifest_path = arg_value(args, "--manifest")?;
    let execution_id = arg_value(args, "--execution-id")?;
    let marker_mode = arg_value(args, "--marker-mode").unwrap_or_else(|_| "expected".to_string());
    let fault = arg_value(args, "--fault").unwrap_or_else(|_| "none".to_string());
    let input_json =
        arg_value(args, "--input-json").unwrap_or_else(|_| DEFAULT_INPUT_JSON.to_string());

    let manifest: Manifest = serde_json::from_slice(&fs::read(manifest_path)?)?;
    let image_id_bytes = decode_image_id(&manifest.image_id)?;
    let framed_input = framed_input(input_json.as_bytes());
    let execution_config_input_hash = risc0_sha256(&framed_input);
    let framed_input_hex = hex::encode(framed_input);
    let committed_outputs = reducer_committed_outputs(&input_json)?;
    let callback_input_digest = execution_config_input_hash;
    let committed_outputs_digest = solana_sha256(&committed_outputs);
    let journal_hash = solana_hashv(&[&callback_input_digest, committed_outputs.as_slice()]);

    let rpc = RpcClient::new_with_commitment(rpc_url, CommitmentConfig::confirmed());
    let client = read_keypair_file(&keypair_path)?;
    fund_if_needed(&rpc, &client.pubkey(), LAMPORTS_PER_SOL)?;
    let setup = setup_production_protocol(
        &rpc,
        &client,
        &execution_id,
        image_id_bytes,
        callback_input_digest,
        committed_outputs_digest,
        journal_hash,
        committed_outputs.as_slice(),
    )?;

    let program_id = Pubkey::from_str(KSWARM_PROGRAM_ID)?;
    let execution_id_bytes = fixed_execution_id_bytes(&execution_id)?;
    let aggregate_job = Pubkey::from_str(&setup.aggregate_job)?;
    let expected_marker = production_marker_pda(
        &program_id,
        &aggregate_job,
        &execution_id_bytes,
        &image_id_bytes,
        &callback_input_digest,
        &journal_hash,
    );
    let marker = if marker_mode == "wrong" {
        let mut wrong_journal_hash = journal_hash;
        wrong_journal_hash[0] ^= 0xff;
        production_marker_pda(
            &program_id,
            &aggregate_job,
            &execution_id_bytes,
            &image_id_bytes,
            &callback_input_digest,
            &wrong_journal_hash,
        )
    } else {
        expected_marker
    };
    let bonsol_program = Pubkey::from_str(BONSOL_PROGRAM_ID)?;
    let execution_account = execution_address(&client.pubkey(), &execution_id, &bonsol_program);

    let mut callback_args_output_digest = committed_outputs_digest;
    if fault == "output-digest" {
        callback_args_output_digest[0] ^= 0xff;
    } else if fault != "none" {
        return Err("production fault must be none or output-digest".into());
    }
    let prefix = encode_record_aggregate_verification(
        execution_id_bytes,
        image_id_bytes,
        callback_input_digest,
        callback_args_output_digest,
        journal_hash,
    );

    let prepared = PreparedExecution {
        image_id: manifest.image_id,
        image_id_bytes_hex: hex::encode(image_id_bytes),
        execution_id,
        input_json,
        framed_input_hex,
        execution_config_input_hash: hex::encode(execution_config_input_hash),
        callback_input_digest: hex::encode(callback_input_digest),
        committed_outputs: hex::encode(committed_outputs),
        committed_outputs_digest: hex::encode(committed_outputs_digest),
        marker_pda: marker.to_string(),
        expected_marker_pda: expected_marker.to_string(),
        execution_account: Some(execution_account.to_string()),
        callback_program_id: KSWARM_PROGRAM_ID.to_string(),
        callback_instruction_prefix: prefix,
        aggregate_job: Some(setup.aggregate_job),
        job_escrow_vault: Some(setup.job_escrow_vault),
        payment_mint: Some(setup.payment_mint),
        token_program: Some(setup.token_program),
        worker_authority: Some(setup.worker_authority),
        worker: Some(setup.worker),
        worker_payment_account: Some(setup.worker_payment_account),
        worker_stake_vault: Some(setup.worker_stake_vault),
        verifier_authority: Some(setup.verifier_authority),
        verifier: Some(setup.verifier),
        verifier_stake_vault: Some(setup.verifier_stake_vault),
        challenge_deadline_unix: Some(setup.challenge_deadline_unix),
        reward_amount: Some(setup.reward_amount),
    };

    println!("{}", serde_json::to_string_pretty(&prepared)?);
    Ok(())
}

fn verify_production_marker(args: &[String]) -> Result<(), Box<dyn std::error::Error>> {
    let prepared_path = arg_value(args, "--prepared")?;
    let marker_path = arg_value(args, "--marker-bin")?;
    let prepared: PreparedExecution = serde_json::from_slice(&fs::read(prepared_path)?)?;
    let marker = fs::read(marker_path)?;
    if marker.len() != BONSOL_AGGREGATE_VERIFICATION_LEN {
        return Err(format!(
            "production marker length mismatch: got {}, expected {}",
            marker.len(),
            BONSOL_AGGREGATE_VERIFICATION_LEN
        )
        .into());
    }
    let expected_discriminator = anchor_account_discriminator("BonsolAggregateVerification");
    if marker.get(0..8) != Some(expected_discriminator.as_slice()) {
        return Err("production marker discriminator mismatch".into());
    }
    let aggregate_job = prepared
        .aggregate_job
        .as_ref()
        .ok_or("prepared production marker is missing aggregateJob")?;
    check_pubkey("aggregate job", &marker[9..41], aggregate_job)?;
    check_hex(
        "execution id",
        &marker[41..73],
        &hex::encode(fixed_execution_id_bytes(&prepared.execution_id)?),
    )?;
    check_hex("image id", &marker[73..105], &prepared.image_id_bytes_hex)?;
    check_hex(
        "input digest",
        &marker[105..137],
        &prepared.callback_input_digest,
    )?;
    check_hex(
        "output digest",
        &marker[137..169],
        &prepared.committed_outputs_digest,
    )?;
    let input_digest = hex::decode(&prepared.callback_input_digest)?;
    let committed_outputs = hex::decode(&prepared.committed_outputs)?;
    let journal_hash = solana_hashv(&[input_digest.as_slice(), committed_outputs.as_slice()]);
    check_hex(
        "journal hash",
        &marker[169..201],
        &hex::encode(journal_hash),
    )?;
    if marker[209] != 1 {
        return Err(format!("production marker status mismatch: got {}", marker[209]).into());
    }
    println!("production marker verified");
    Ok(())
}

fn settle_production(args: &[String]) -> Result<(), Box<dyn std::error::Error>> {
    let rpc_url = arg_value(args, "--rpc-url")?;
    let keypair_path = arg_value(args, "--keypair")?;
    let prepared_path = arg_value(args, "--prepared")?;
    let marker = Pubkey::from_str(&arg_value(args, "--marker")?)?;
    let prepared: PreparedExecution = serde_json::from_slice(&fs::read(prepared_path)?)?;
    let rpc = RpcClient::new_with_commitment(rpc_url, CommitmentConfig::confirmed());
    let client = read_keypair_file(keypair_path)?;
    let worker_payment_account = Pubkey::from_str(
        prepared
            .worker_payment_account
            .as_ref()
            .ok_or("prepared production marker is missing workerPaymentAccount")?,
    )?;
    let before = token_balance(&rpc, &worker_payment_account)?;
    wait_for_challenge_window(&prepared)?;

    let program_id = Pubkey::from_str(KSWARM_PROGRAM_ID)?;
    let config = config_pda(&program_id);
    let payment = read_config(&rpc, &config)?.ok_or("protocol config is not initialized")?;
    let prepared_mint = Pubkey::from_str(prepared.payment_mint.as_ref().ok_or("missing paymentMint")?)?;
    if prepared_mint != payment.mint {
        return Err(format!(
            "prepared paymentMint {} does not match the on-chain config mint {}",
            prepared_mint, payment.mint
        )
        .into());
    }
    let ix = Instruction::new_with_bytes(
        program_id,
        &anchor_instruction_discriminator("settle_aggregate_proof_job"),
        vec![
            AccountMeta::new(client.pubkey(), true),
            AccountMeta::new_readonly(config, false),
            AccountMeta::new_readonly(payment.mint, false),
            AccountMeta::new(
                Pubkey::from_str(
                    prepared
                        .aggregate_job
                        .as_ref()
                        .ok_or("missing aggregateJob")?,
                )?,
                false,
            ),
            AccountMeta::new_readonly(marker, false),
            AccountMeta::new(
                Pubkey::from_str(prepared.worker.as_ref().ok_or("missing worker")?)?,
                false,
            ),
            AccountMeta::new_readonly(
                Pubkey::from_str(
                    prepared
                        .worker_authority
                        .as_ref()
                        .ok_or("missing workerAuthority")?,
                )?,
                false,
            ),
            AccountMeta::new(
                Pubkey::from_str(
                    prepared
                        .job_escrow_vault
                        .as_ref()
                        .ok_or("missing jobEscrowVault")?,
                )?,
                false,
            ),
            AccountMeta::new(worker_payment_account, false),
            AccountMeta::new_readonly(payment.token_program, false),
        ],
    );
    let signature = send_tx(&rpc, &[ix], &client, &[])?;
    let after = token_balance(&rpc, &worker_payment_account)?;
    let reward_amount = prepared.reward_amount.ok_or("missing rewardAmount")?;
    if after < before.saturating_add(reward_amount) {
        return Err(format!(
            "worker payment did not increase by reward: before={} after={} reward={}",
            before, after, reward_amount
        )
        .into());
    }
    println!(
        "{}",
        serde_json::to_string_pretty(&serde_json::json!({
            "settleSignature": signature.to_string(),
            "workerPaymentAccount": worker_payment_account.to_string(),
            "workerBalanceBefore": before,
            "workerBalanceAfter": after,
            "rewardAmount": reward_amount
        }))?
    );
    Ok(())
}

fn execute_wrong_input_hash(args: &[String]) -> Result<(), Box<dyn std::error::Error>> {
    let rpc_url = arg_value(args, "--rpc-url")?;
    let keypair_path = arg_value(args, "--keypair")?;
    let manifest_path = arg_value(args, "--manifest")?;
    let execution_id = arg_value(args, "--execution-id")?;
    let input_json =
        arg_value(args, "--input-json").unwrap_or_else(|_| DEFAULT_INPUT_JSON.to_string());
    let manifest: Manifest = serde_json::from_slice(&fs::read(manifest_path)?)?;
    let rpc = RpcClient::new_with_commitment(rpc_url, CommitmentConfig::confirmed());
    let signer = read_keypair_file(keypair_path)?;
    let requester = signer.pubkey();
    let bonsol_program = Pubkey::from_str(BONSOL_PROGRAM_ID)?;
    let execution_account = execution_address(&requester, &execution_id, &bonsol_program);
    let deployment_account = deployment_address(&manifest.image_id, &bonsol_program);
    let current_slot = rpc.get_slot()?;
    let expiry = current_slot + 1500;
    let wrong_input_hash = [0xa5u8; 32];
    let ix_data = build_execute_v1_instruction_data(
        &manifest.image_id,
        &execution_id,
        input_json.as_bytes(),
        &wrong_input_hash,
        expiry,
    );
    let accounts = vec![
        AccountMeta::new(requester, true),
        AccountMeta::new(requester, true),
        AccountMeta::new(execution_account, false),
        AccountMeta::new_readonly(deployment_account, false),
        AccountMeta::new_readonly(bonsol_program, false),
        AccountMeta::new_readonly(system_program::ID, false),
    ];
    let instructions = vec![
        ComputeBudgetInstruction::set_compute_unit_limit(80_000),
        Instruction::new_with_bytes(bonsol_program, &ix_data, accounts),
    ];
    let (blockhash, _) = rpc.get_latest_blockhash_with_commitment(CommitmentConfig::processed())?;
    let tx =
        Transaction::new_signed_with_payer(&instructions, Some(&requester), &[&signer], blockhash);
    let signature = rpc.send_and_confirm_transaction(&tx)?;
    let result = serde_json::json!({
        "signature": signature.to_string(),
        "executionId": execution_id,
        "executionAccount": execution_account.to_string(),
        "wrongInputHash": hex::encode(wrong_input_hash),
        "fundingLamports": LAMPORTS_PER_SOL / 100,
    });
    println!("{}", serde_json::to_string_pretty(&result)?);
    Ok(())
}

fn replay_status(args: &[String]) -> Result<(), Box<dyn std::error::Error>> {
    let rpc_url = arg_value(args, "--rpc-url")?;
    let keypair_path = arg_value(args, "--keypair")?;
    let tx_json_path = arg_value(args, "--tx-json")?;
    let mode = arg_value(args, "--mode").unwrap_or_else(|_| "replay".to_string());
    let rpc = RpcClient::new_with_commitment(rpc_url, CommitmentConfig::confirmed());
    let signer = read_keypair_file(keypair_path)?;
    let tx_json: serde_json::Value = serde_json::from_slice(&fs::read(tx_json_path)?)?;
    let (program_id, mut metas, data) = extract_instruction_from_transaction_json(&tx_json)?;
    if mode == "wrong-execution-account" {
        if metas.len() < 2 {
            return Err("status instruction has fewer than two accounts".into());
        }
        metas[1] = AccountMeta::new(metas[0].pubkey, false);
    } else if mode != "replay" {
        return Err("mode must be replay or wrong-execution-account".into());
    }
    let instruction = Instruction::new_with_bytes(program_id, &data, metas);
    let blockhash = rpc.get_latest_blockhash()?;
    let tx = Transaction::new_signed_with_payer(
        &[instruction],
        Some(&signer.pubkey()),
        &[&signer],
        blockhash,
    );
    let result = rpc.send_and_confirm_transaction_with_spinner_and_config(
        &tx,
        CommitmentConfig::confirmed(),
        RpcSendTransactionConfig {
            skip_preflight: true,
            max_retries: Some(0),
            ..RpcSendTransactionConfig::default()
        },
    );
    let output = match result {
        Ok(signature) => serde_json::json!({
            "mode": mode,
            "accepted": true,
            "signature": signature.to_string()
        }),
        Err(error) => serde_json::json!({
            "mode": mode,
            "accepted": false,
            "error": error.to_string()
        }),
    };
    println!("{}", serde_json::to_string_pretty(&output)?);
    Ok(())
}

fn reducer_committed_outputs(input_json: &str) -> Result<Vec<u8>, Box<dyn std::error::Error>> {
    let value: serde_json::Value = serde_json::from_str(input_json)?;
    let branch_key = value["branch_key"].as_str().unwrap_or_default();
    let child_job_id = value["child_job_id"].as_str().unwrap_or_default();
    let parent_request_id = value["parent_request_id"].as_str().unwrap_or_default();
    let line_count = value["line_count"].as_u64().unwrap_or_default() as u32;
    let word_count = value["word_count"].as_u64().unwrap_or_default() as u32;
    let score_hex = value["score_hex"]
        .as_str()
        .ok_or("score_hex must be a string of 64 lowercase hex digits")?;
    let score = decode_score_felt(score_hex).map_err(|error| format!("score_hex rejected: {error}"))?;
    let canonical = reducer_canonical_bytes(
        branch_key,
        child_job_id,
        parent_request_id,
        score_hex,
        line_count,
        word_count,
    );
    let reducer_digest = risc0_sha256(&canonical);
    Ok(committed_outputs(&reducer_digest, line_count, word_count, &score).to_vec())
}

fn framed_input(input_data: &[u8]) -> Vec<u8> {
    let mut framed = Vec::with_capacity(8 + input_data.len());
    framed.extend_from_slice(&(input_data.len() as u64).to_le_bytes());
    framed.extend_from_slice(input_data);
    framed
}

fn production_marker_pda(
    program_id: &Pubkey,
    aggregate_job: &Pubkey,
    execution_id: &[u8; 32],
    image_id: &[u8; 32],
    input_digest: &[u8; 32],
    journal_hash: &[u8; 32],
) -> Pubkey {
    Pubkey::find_program_address(
        &[
            b"bonsol_aggregate_verification",
            aggregate_job.as_ref(),
            execution_id,
            image_id,
            input_digest,
            journal_hash,
        ],
        program_id,
    )
    .0
}

/// Reads `payment_mint` and `token_program` from an existing `ProtocolConfig`
/// (layout: 8-byte discriminator, bump u8, admin, payment_mint, token_program, ...).
fn read_config(
    rpc: &RpcClient,
    config: &Pubkey,
) -> Result<Option<PaymentMint>, Box<dyn std::error::Error>> {
    let account = match rpc.get_account(config) {
        Ok(account) => account,
        Err(_) => return Ok(None),
    };
    if account.data.len() < 105 {
        return Err("existing protocol config account is too short".into());
    }
    let mut mint = [0u8; 32];
    mint.copy_from_slice(&account.data[41..73]);
    let mut token_program = [0u8; 32];
    token_program.copy_from_slice(&account.data[73..105]);
    Ok(Some(PaymentMint {
        mint: Pubkey::new_from_array(mint),
        token_program: Pubkey::new_from_array(token_program),
    }))
}

fn setup_production_protocol(
    rpc: &RpcClient,
    client: &Keypair,
    execution_id: &str,
    image_id: [u8; 32],
    input_digest: [u8; 32],
    output_digest: [u8; 32],
    journal_hash: [u8; 32],
    committed_outputs: &[u8],
) -> Result<ProductionSetup, Box<dyn std::error::Error>> {
    let program_id = Pubkey::from_str(KSWARM_PROGRAM_ID)?;
    let config = config_pda(&program_id);
    let existing = read_config(rpc, &config)?;
    let new_mint = Keypair::new();
    let payment = existing.unwrap_or(PaymentMint {
        mint: new_mint.pubkey(),
        token_program: spl_token::ID,
    });
    if existing.is_none() {
        eprintln!("prepare-production: create stand-in payment mint (classic SPL Token, 6 decimals)");
        create_payment_mint(rpc, client, &new_mint)?;
    } else {
        eprintln!("prepare-production: reuse configured payment mint");
    }

    let customer_ata = token_ata(&payment, &client.pubkey());
    eprintln!("prepare-production: create customer token account");
    create_ata(rpc, client, &payment, &client.pubkey())?;

    let worker_authority = Keypair::new();
    let verifier_authority = Keypair::new();
    fund_if_needed(rpc, &worker_authority.pubkey(), LAMPORTS_PER_SOL)?;
    fund_if_needed(rpc, &verifier_authority.pubkey(), LAMPORTS_PER_SOL)?;
    let worker_funding_ata = token_ata(&payment, &worker_authority.pubkey());
    let verifier_funding_ata = token_ata(&payment, &verifier_authority.pubkey());
    eprintln!("prepare-production: create worker/verifier funding token accounts");
    create_ata(rpc, client, &payment, &worker_authority.pubkey())?;
    create_ata(rpc, client, &payment, &verifier_authority.pubkey())?;

    eprintln!("prepare-production: mint funding balances");
    mint_to(rpc, client, &payment, &customer_ata, REWARD_AMOUNT * 2)?;
    mint_to(
        rpc,
        client,
        &payment,
        &worker_funding_ata,
        WORKER_STAKE_DEPOSIT,
    )?;
    mint_to(
        rpc,
        client,
        &payment,
        &verifier_funding_ata,
        VERIFIER_STAKE_DEPOSIT,
    )?;

    if existing.is_none() {
        eprintln!("prepare-production: initialize protocol");
        let mut data = anchor_instruction_discriminator("initialize_protocol");
        data.extend_from_slice(&TIER_ONE_STAKE_FLOOR.to_le_bytes());
        data.extend_from_slice(&TIER_TWO_STAKE_FLOOR.to_le_bytes());
        data.extend_from_slice(&TIER_THREE_STAKE_FLOOR.to_le_bytes());
        data.extend_from_slice(&VERIFIER_STAKE_FLOOR.to_le_bytes());
        data.extend_from_slice(&MIN_CHALLENGE_WINDOW_SECONDS.to_le_bytes());
        send_tx(
            rpc,
            &[Instruction::new_with_bytes(
                program_id,
                &data,
                vec![
                    AccountMeta::new(client.pubkey(), true),
                    AccountMeta::new(config, false),
                    AccountMeta::new_readonly(payment.mint, false),
                    AccountMeta::new_readonly(payment.token_program, false),
                    AccountMeta::new_readonly(system_program::ID, false),
                    // The program and its ProgramData: `initialize_protocol` accepts
                    // only the upgrade authority, so `client` must be the authority of
                    // the deployed program (deploy with `--upgradeable-program`).
                    AccountMeta::new_readonly(program_id, false),
                    AccountMeta::new_readonly(program_data_pda(&program_id), false),
                ],
            )],
            client,
            &[],
        )?;
    } else {
        eprintln!("prepare-production: protocol already initialized");
    }

    let worker = worker_pda(&program_id, &worker_authority.pubkey());
    let worker_stake_vault = token_ata(&payment, &worker);
    eprintln!("prepare-production: register aggregate worker");
    register_worker(
        rpc,
        client,
        &worker_authority,
        program_id,
        config,
        payment,
        worker,
        worker_stake_vault,
        2,
        AGGREGATE_PROOF_CAPABILITY_HASH,
        image_id,
    )?;
    eprintln!("prepare-production: deposit worker stake");
    deposit_stake(
        rpc,
        &worker_authority,
        program_id,
        config,
        payment,
        worker,
        worker_stake_vault,
        worker_funding_ata,
        WORKER_STAKE_DEPOSIT,
    )?;

    let verifier = worker_pda(&program_id, &verifier_authority.pubkey());
    let verifier_stake_vault = token_ata(&payment, &verifier);
    eprintln!("prepare-production: register verifier");
    register_worker(
        rpc,
        client,
        &verifier_authority,
        program_id,
        config,
        payment,
        verifier,
        verifier_stake_vault,
        10,
        ZERO_HASH,
        image_id,
    )?;
    eprintln!("prepare-production: deposit verifier stake");
    deposit_stake(
        rpc,
        &verifier_authority,
        program_id,
        config,
        payment,
        verifier,
        verifier_stake_vault,
        verifier_funding_ata,
        VERIFIER_STAKE_DEPOSIT,
    )?;

    let mut nonce_bytes = [0u8; 8];
    nonce_bytes.copy_from_slice(&hash(execution_id.as_bytes()).to_bytes()[..8]);
    let nonce = u64::from_le_bytes(nonce_bytes);
    let job = job_pda(&program_id, &client.pubkey(), nonce);
    let job_escrow_vault = token_ata(&payment, &job);
    eprintln!("prepare-production: open aggregate job");
    open_aggregate_job(
        rpc,
        client,
        program_id,
        config,
        payment,
        job,
        job_escrow_vault,
        customer_ata,
        nonce,
        input_digest,
        journal_hash,
        image_id,
    )?;
    eprintln!("prepare-production: commit input artifact");
    commit_input_artifact(rpc, client, program_id, job)?;
    eprintln!("prepare-production: claim job");
    claim_job(
        rpc,
        &worker_authority,
        program_id,
        config,
        payment,
        worker,
        worker_stake_vault,
        job,
    )?;
    eprintln!("prepare-production: submit receipt");
    submit_receipt(
        rpc,
        &worker_authority,
        program_id,
        worker,
        job,
        committed_outputs,
    )?;
    eprintln!("prepare-production: assign verifier");
    assign_verifier(
        rpc,
        client,
        program_id,
        config,
        job,
        verifier_authority.pubkey(),
    )?;
    eprintln!("prepare-production: submit verifier attestation");
    submit_verifier_attestation(
        rpc,
        &verifier_authority,
        program_id,
        config,
        payment,
        verifier,
        verifier_stake_vault,
        job,
        output_digest,
        image_id,
    )?;
    eprintln!("prepare-production: protocol setup complete");

    Ok(ProductionSetup {
        aggregate_job: job.to_string(),
        job_escrow_vault: job_escrow_vault.to_string(),
        payment_mint: payment.mint.to_string(),
        token_program: payment.token_program.to_string(),
        worker_authority: worker_authority.pubkey().to_string(),
        worker: worker.to_string(),
        worker_payment_account: worker_funding_ata.to_string(),
        worker_stake_vault: worker_stake_vault.to_string(),
        verifier_authority: verifier_authority.pubkey().to_string(),
        verifier: verifier.to_string(),
        verifier_stake_vault: verifier_stake_vault.to_string(),
        challenge_deadline_unix: unix_now()?.saturating_add(5),
        reward_amount: REWARD_AMOUNT,
    })
}

/// Creates a classic SPL Token mint with `TOKEN_DECIMALS` decimals; the `spl_token_2022`
/// instruction builders accept the classic program id and share its wire layout.
fn create_payment_mint(
    rpc: &RpcClient,
    payer: &Keypair,
    mint: &Keypair,
) -> Result<(), Box<dyn std::error::Error>> {
    let mint_space = ExtensionType::try_calculate_account_len::<Mint>(&[])?;
    let lamports = rpc.get_minimum_balance_for_rent_exemption(mint_space)?;
    send_tx(
        rpc,
        &[
            system_instruction::create_account(
                &payer.pubkey(),
                &mint.pubkey(),
                lamports,
                mint_space as u64,
                &spl_token::ID,
            ),
            token_2022_instruction::initialize_mint2(
                &spl_token::ID,
                &mint.pubkey(),
                &payer.pubkey(),
                None,
                TOKEN_DECIMALS,
            )?,
        ],
        payer,
        &[mint],
    )?;
    Ok(())
}

fn create_ata(
    rpc: &RpcClient,
    payer: &Keypair,
    payment: &PaymentMint,
    owner: &Pubkey,
) -> Result<(), Box<dyn std::error::Error>> {
    if rpc.get_account(&token_ata(payment, owner)).is_ok() {
        return Ok(());
    }
    send_tx(
        rpc,
        &[create_associated_token_account(
            &payer.pubkey(),
            owner,
            &payment.mint,
            &payment.token_program,
        )],
        payer,
        &[],
    )?;
    Ok(())
}

fn mint_to(
    rpc: &RpcClient,
    authority: &Keypair,
    payment: &PaymentMint,
    destination: &Pubkey,
    amount: u64,
) -> Result<(), Box<dyn std::error::Error>> {
    send_tx(
        rpc,
        &[token_2022_instruction::mint_to(
            &payment.token_program,
            &payment.mint,
            destination,
            &authority.pubkey(),
            &[],
            amount,
        )?],
        authority,
        &[],
    )?;
    Ok(())
}

fn register_worker(
    rpc: &RpcClient,
    payer: &Keypair,
    authority: &Keypair,
    program_id: Pubkey,
    config: Pubkey,
    payment: PaymentMint,
    worker: Pubkey,
    stake_vault: Pubkey,
    role: u8,
    capability: [u8; 32],
    software_digest: [u8; 32],
) -> Result<(), Box<dyn std::error::Error>> {
    let mut data = anchor_instruction_discriminator("register_worker");
    data.push(role);
    data.extend_from_slice(&capability);
    data.extend_from_slice(&software_digest);
    send_tx(
        rpc,
        &[Instruction::new_with_bytes(
            program_id,
            &data,
            vec![
                AccountMeta::new(authority.pubkey(), true),
                AccountMeta::new_readonly(config, false),
                AccountMeta::new_readonly(payment.mint, false),
                AccountMeta::new(worker, false),
                AccountMeta::new(stake_vault, false),
                AccountMeta::new_readonly(payment.token_program, false),
                AccountMeta::new_readonly(spl_associated_token_account::ID, false),
                AccountMeta::new_readonly(system_program::ID, false),
            ],
        )],
        payer,
        &[authority],
    )?;
    Ok(())
}

fn deposit_stake(
    rpc: &RpcClient,
    authority: &Keypair,
    program_id: Pubkey,
    config: Pubkey,
    payment: PaymentMint,
    worker: Pubkey,
    stake_vault: Pubkey,
    funding_account: Pubkey,
    amount: u64,
) -> Result<(), Box<dyn std::error::Error>> {
    let mut data = anchor_instruction_discriminator("deposit_worker_stake");
    data.extend_from_slice(&amount.to_le_bytes());
    send_tx(
        rpc,
        &[Instruction::new_with_bytes(
            program_id,
            &data,
            vec![
                AccountMeta::new(authority.pubkey(), true),
                AccountMeta::new_readonly(config, false),
                AccountMeta::new_readonly(payment.mint, false),
                AccountMeta::new_readonly(worker, false),
                AccountMeta::new(stake_vault, false),
                AccountMeta::new(funding_account, false),
                AccountMeta::new_readonly(payment.token_program, false),
            ],
        )],
        authority,
        &[],
    )?;
    Ok(())
}

fn open_aggregate_job(
    rpc: &RpcClient,
    customer: &Keypair,
    program_id: Pubkey,
    config: Pubkey,
    payment: PaymentMint,
    job: Pubkey,
    job_escrow_vault: Pubkey,
    customer_token: Pubkey,
    nonce: u64,
    input_digest: [u8; 32],
    journal_hash: [u8; 32],
    image_id: [u8; 32],
) -> Result<(), Box<dyn std::error::Error>> {
    let mut data = anchor_instruction_discriminator("open_job");
    data.extend_from_slice(&nonce.to_le_bytes());
    data.extend_from_slice(&input_digest);
    data.extend_from_slice(&journal_hash);
    data.extend_from_slice(&REWARD_AMOUNT.to_le_bytes());
    data.extend_from_slice(&REQUIRED_STAKE.to_le_bytes());
    data.push(4);
    data.push(2);
    data.push(2);
    data.extend_from_slice(&AGGREGATE_PROOF_CAPABILITY_HASH);
    data.extend_from_slice(&image_id);
    data.extend_from_slice(&90u32.to_le_bytes());
    data.extend_from_slice(&90u32.to_le_bytes());
    data.extend_from_slice(&MIN_CHALLENGE_WINDOW_SECONDS.to_le_bytes());
    data.extend_from_slice(&CHALLENGE_BOND.to_le_bytes());
    send_tx(
        rpc,
        &[Instruction::new_with_bytes(
            program_id,
            &data,
            vec![
                AccountMeta::new(customer.pubkey(), true),
                AccountMeta::new_readonly(config, false),
                AccountMeta::new_readonly(payment.mint, false),
                AccountMeta::new(job, false),
                AccountMeta::new(job_escrow_vault, false),
                AccountMeta::new(customer_token, false),
                AccountMeta::new_readonly(payment.token_program, false),
                AccountMeta::new_readonly(spl_associated_token_account::ID, false),
                AccountMeta::new_readonly(system_program::ID, false),
            ],
        )],
        customer,
        &[],
    )?;
    Ok(())
}

fn commit_input_artifact(
    rpc: &RpcClient,
    customer: &Keypair,
    program_id: Pubkey,
    job: Pubkey,
) -> Result<(), Box<dyn std::error::Error>> {
    let mut data = anchor_instruction_discriminator("commit_input_artifact");
    encode_string(&mut data, "bafkreiproductioncallbackinputartifactv1");
    send_tx(
        rpc,
        &[Instruction::new_with_bytes(
            program_id,
            &data,
            vec![
                AccountMeta::new(customer.pubkey(), true),
                AccountMeta::new(job, false),
            ],
        )],
        customer,
        &[],
    )?;
    Ok(())
}

fn claim_job(
    rpc: &RpcClient,
    worker_authority: &Keypair,
    program_id: Pubkey,
    config: Pubkey,
    payment: PaymentMint,
    worker: Pubkey,
    worker_stake_vault: Pubkey,
    job: Pubkey,
) -> Result<(), Box<dyn std::error::Error>> {
    send_tx(
        rpc,
        &[Instruction::new_with_bytes(
            program_id,
            &anchor_instruction_discriminator("claim_job"),
            vec![
                AccountMeta::new(worker_authority.pubkey(), true),
                AccountMeta::new_readonly(config, false),
                AccountMeta::new_readonly(payment.mint, false),
                AccountMeta::new(worker, false),
                AccountMeta::new(worker_stake_vault, false),
                AccountMeta::new(job, false),
                AccountMeta::new_readonly(payment.token_program, false),
            ],
        )],
        worker_authority,
        &[],
    )?;
    Ok(())
}

fn submit_receipt(
    rpc: &RpcClient,
    worker_authority: &Keypair,
    program_id: Pubkey,
    worker: Pubkey,
    job: Pubkey,
    committed_outputs: &[u8],
) -> Result<(), Box<dyn std::error::Error>> {
    let mut data = anchor_instruction_discriminator("submit_receipt");
    encode_string(&mut data, "bafkreiproductioncallbackoutputartifactv1");
    encode_vec(&mut data, committed_outputs);
    send_tx(
        rpc,
        &[Instruction::new_with_bytes(
            program_id,
            &data,
            vec![
                AccountMeta::new(worker_authority.pubkey(), true),
                AccountMeta::new(worker, false),
                AccountMeta::new(job, false),
            ],
        )],
        worker_authority,
        &[],
    )?;
    Ok(())
}

fn assign_verifier(
    rpc: &RpcClient,
    customer: &Keypair,
    program_id: Pubkey,
    config: Pubkey,
    job: Pubkey,
    verifier_authority: Pubkey,
) -> Result<(), Box<dyn std::error::Error>> {
    let mut data = anchor_instruction_discriminator("assign_verifier");
    data.extend_from_slice(verifier_authority.as_ref());
    send_tx(
        rpc,
        &[Instruction::new_with_bytes(
            program_id,
            &data,
            vec![
                AccountMeta::new(customer.pubkey(), true),
                AccountMeta::new_readonly(config, false),
                AccountMeta::new(job, false),
            ],
        )],
        customer,
        &[],
    )?;
    Ok(())
}

fn submit_verifier_attestation(
    rpc: &RpcClient,
    verifier_authority: &Keypair,
    program_id: Pubkey,
    config: Pubkey,
    payment: PaymentMint,
    verifier: Pubkey,
    verifier_stake_vault: Pubkey,
    job: Pubkey,
    result_hash: [u8; 32],
    software_digest: [u8; 32],
) -> Result<(), Box<dyn std::error::Error>> {
    let mut data = anchor_instruction_discriminator("submit_verifier_attestation");
    data.extend_from_slice(&result_hash);
    encode_string(&mut data, "bafkreiproductioncallbackverifierevidencev1");
    data.extend_from_slice(&software_digest);
    send_tx(
        rpc,
        &[Instruction::new_with_bytes(
            program_id,
            &data,
            vec![
                AccountMeta::new(verifier_authority.pubkey(), true),
                AccountMeta::new_readonly(config, false),
                AccountMeta::new_readonly(payment.mint, false),
                AccountMeta::new_readonly(verifier, false),
                AccountMeta::new_readonly(verifier_stake_vault, false),
                AccountMeta::new(job, false),
                AccountMeta::new_readonly(payment.token_program, false),
            ],
        )],
        verifier_authority,
        &[],
    )?;
    Ok(())
}

fn send_tx(
    rpc: &RpcClient,
    instructions: &[Instruction],
    payer: &Keypair,
    additional_signers: &[&Keypair],
) -> Result<solana_sdk::signature::Signature, Box<dyn std::error::Error>> {
    let blockhash = rpc.get_latest_blockhash()?;
    let mut signers = Vec::with_capacity(1 + additional_signers.len());
    signers.push(payer);
    signers.extend_from_slice(additional_signers);
    let tx = Transaction::new_signed_with_payer(
        instructions,
        Some(&payer.pubkey()),
        signers.as_slice(),
        blockhash,
    );
    Ok(rpc.send_and_confirm_transaction_with_spinner_and_config(
        &tx,
        CommitmentConfig::confirmed(),
        RpcSendTransactionConfig {
            skip_preflight: true,
            max_retries: Some(5),
            ..RpcSendTransactionConfig::default()
        },
    )?)
}

fn fund_if_needed(
    rpc: &RpcClient,
    pubkey: &Pubkey,
    minimum_lamports: u64,
) -> Result<(), Box<dyn std::error::Error>> {
    if rpc.get_balance(pubkey).unwrap_or(0) >= minimum_lamports {
        return Ok(());
    }
    let signature = rpc.request_airdrop(pubkey, minimum_lamports)?;
    rpc.confirm_transaction(&signature)?;
    Ok(())
}

fn token_balance(
    rpc: &RpcClient,
    token_account: &Pubkey,
) -> Result<u64, Box<dyn std::error::Error>> {
    Ok(rpc
        .get_token_account_balance(token_account)?
        .amount
        .parse()?)
}

fn wait_for_challenge_window(
    prepared: &PreparedExecution,
) -> Result<(), Box<dyn std::error::Error>> {
    let deadline = prepared
        .challenge_deadline_unix
        .ok_or("prepared production marker is missing challengeDeadlineUnix")?;
    let now = unix_now()?;
    if deadline >= now {
        std::thread::sleep(std::time::Duration::from_secs((deadline - now + 1) as u64));
    }
    Ok(())
}

fn token_ata(payment: &PaymentMint, owner: &Pubkey) -> Pubkey {
    get_associated_token_address_with_program_id(owner, &payment.mint, &payment.token_program)
}

fn config_pda(program_id: &Pubkey) -> Pubkey {
    Pubkey::find_program_address(&[b"config"], program_id).0
}

/// `ProgramData` account of an upgradeable program (seed: the program id).
/// `initialize_protocol` takes it and requires its upgrade authority to sign as admin.
fn program_data_pda(program_id: &Pubkey) -> Pubkey {
    Pubkey::find_program_address(&[program_id.as_ref()], &bpf_loader_upgradeable::ID).0
}

fn worker_pda(program_id: &Pubkey, authority: &Pubkey) -> Pubkey {
    Pubkey::find_program_address(&[b"worker", authority.as_ref()], program_id).0
}

fn job_pda(program_id: &Pubkey, customer: &Pubkey, nonce: u64) -> Pubkey {
    Pubkey::find_program_address(
        &[b"job", customer.as_ref(), &nonce.to_le_bytes()],
        program_id,
    )
    .0
}

fn encode_record_aggregate_verification(
    execution_id: [u8; 32],
    image_id: [u8; 32],
    input_digest: [u8; 32],
    output_digest: [u8; 32],
    journal_hash: [u8; 32],
) -> Vec<u8> {
    let mut data = vec![1];
    data.extend_from_slice(&execution_id);
    data.extend_from_slice(&image_id);
    data.extend_from_slice(&input_digest);
    data.extend_from_slice(&output_digest);
    data.extend_from_slice(&journal_hash);
    data
}

fn anchor_instruction_discriminator(name: &str) -> Vec<u8> {
    let preimage = format!("global:{name}");
    hash(preimage.as_bytes()).to_bytes()[..8].to_vec()
}

fn anchor_account_discriminator(name: &str) -> Vec<u8> {
    let preimage = format!("account:{name}");
    hash(preimage.as_bytes()).to_bytes()[..8].to_vec()
}

fn encode_string(data: &mut Vec<u8>, value: &str) {
    data.extend_from_slice(&(value.len() as u32).to_le_bytes());
    data.extend_from_slice(value.as_bytes());
}

fn encode_vec(data: &mut Vec<u8>, value: &[u8]) {
    data.extend_from_slice(&(value.len() as u32).to_le_bytes());
    data.extend_from_slice(value);
}

fn fixed_execution_id_bytes(execution_id: &str) -> Result<[u8; 32], Box<dyn std::error::Error>> {
    if execution_id.is_empty() || execution_id.len() > 32 {
        return Err("execution id must be 1..=32 bytes for production callback".into());
    }
    let mut out = [0u8; 32];
    out[..execution_id.len()].copy_from_slice(execution_id.as_bytes());
    Ok(out)
}

fn solana_hashv(values: &[&[u8]]) -> [u8; 32] {
    solana_sdk::hash::hashv(values).to_bytes()
}

fn unix_now() -> Result<i64, Box<dyn std::error::Error>> {
    Ok(std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)?
        .as_secs() as i64)
}

fn build_execute_v1_instruction_data(
    image_id: &str,
    execution_id: &str,
    input_data: &[u8],
    input_hash: &[u8; 32],
    expiry: u64,
) -> Vec<u8> {
    let mut fbb = flatbuffers::FlatBufferBuilder::new();
    let framed_input = framed_input(input_data);
    let input_data_vec = fbb.create_vector(&framed_input);
    let input_table = fbb.start_table();
    fbb.push_slot::<u8>(4, 1, 1);
    fbb.push_slot_always(6, input_data_vec);
    let input_table = fbb.end_table(input_table);
    let inputs = fbb.create_vector(&[input_table]);
    let image_id = fbb.create_string(image_id);
    let execution_id = fbb.create_string(execution_id);
    let input_hash = fbb.create_vector(input_hash);
    let table = fbb.start_table();
    fbb.push_slot::<u64>(4, 12_000, 0);
    fbb.push_slot_always(6, execution_id);
    fbb.push_slot_always(8, image_id);
    fbb.push_slot::<bool>(14, true, false);
    fbb.push_slot::<bool>(16, true, true);
    fbb.push_slot_always(18, inputs);
    fbb.push_slot_always(20, input_hash);
    fbb.push_slot::<u64>(22, expiry, 0);
    let execution_request = fbb.end_table(table);
    fbb.finish(execution_request, None);
    let execution_request_bytes = fbb.finished_data().to_vec();

    let mut fbb = flatbuffers::FlatBufferBuilder::new();
    let execution_request = fbb.create_vector(&execution_request_bytes);
    let table = fbb.start_table();
    fbb.push_slot::<u8>(4, 0, 0);
    fbb.push_slot_always(6, execution_request);
    let channel_instruction = fbb.end_table(table);
    fbb.finish(channel_instruction, None);
    fbb.finished_data().to_vec()
}

fn execution_address(requester: &Pubkey, execution_id: &str, bonsol_program: &Pubkey) -> Pubkey {
    Pubkey::find_program_address(
        &[b"execution", requester.as_ref(), execution_id.as_bytes()],
        bonsol_program,
    )
    .0
}

fn deployment_address(image_id: &str, bonsol_program: &Pubkey) -> Pubkey {
    let hash = solana_sdk::keccak::hash(image_id.as_bytes());
    Pubkey::find_program_address(&[b"deployment", hash.as_ref()], bonsol_program).0
}

fn extract_instruction_from_transaction_json(
    tx_json: &serde_json::Value,
) -> Result<(Pubkey, Vec<AccountMeta>, Vec<u8>), Box<dyn std::error::Error>> {
    let message = tx_json
        .pointer("/transaction/message")
        .or_else(|| tx_json.pointer("/transaction/transaction/message"))
        .ok_or("missing transaction message")?;
    let account_keys = message
        .get("accountKeys")
        .and_then(|v| v.as_array())
        .ok_or("missing account keys")?;
    let instructions = message
        .get("instructions")
        .or_else(|| message.get("compiledInstructions"))
        .and_then(|v| v.as_array())
        .ok_or("missing instructions")?;
    let bonsol_program = Pubkey::from_str(BONSOL_PROGRAM_ID)?;

    for instruction in instructions {
        let program_id = instruction_program_id(instruction, account_keys)?;
        if program_id != bonsol_program {
            continue;
        }
        let account_values = instruction
            .get("accounts")
            .or_else(|| instruction.get("accountKeyIndexes"))
            .and_then(|v| v.as_array())
            .ok_or("missing instruction accounts")?;
        let mut metas = Vec::with_capacity(account_values.len());
        for (idx, account_value) in account_values.iter().enumerate() {
            let pubkey = instruction_account_pubkey(account_value, account_keys)?;
            let (is_signer, is_writable) =
                account_flags(&pubkey, account_keys).unwrap_or_else(|| inferred_status_flags(idx));
            metas.push(if is_writable {
                AccountMeta::new(pubkey, is_signer)
            } else {
                AccountMeta::new_readonly(pubkey, is_signer)
            });
        }
        let data = decode_instruction_data(instruction.get("data").ok_or("missing data")?)?;
        return Ok((program_id, metas, data));
    }

    Err("no Bonsol instruction found in transaction JSON".into())
}

fn instruction_program_id(
    instruction: &serde_json::Value,
    account_keys: &[serde_json::Value],
) -> Result<Pubkey, Box<dyn std::error::Error>> {
    if let Some(program_id) = instruction.get("programId").and_then(|v| v.as_str()) {
        return Ok(Pubkey::from_str(program_id)?);
    }
    if let Some(program_id_index) = instruction.get("programIdIndex").and_then(|v| v.as_u64()) {
        return account_key_at(account_keys, program_id_index as usize);
    }
    Err("instruction missing program id".into())
}

fn instruction_account_pubkey(
    account_value: &serde_json::Value,
    account_keys: &[serde_json::Value],
) -> Result<Pubkey, Box<dyn std::error::Error>> {
    if let Some(pubkey) = account_value.as_str() {
        return Ok(Pubkey::from_str(pubkey)?);
    }
    if let Some(index) = account_value.as_u64() {
        return account_key_at(account_keys, index as usize);
    }
    Err("invalid instruction account value".into())
}

fn account_key_at(
    account_keys: &[serde_json::Value],
    index: usize,
) -> Result<Pubkey, Box<dyn std::error::Error>> {
    let value = account_keys
        .get(index)
        .ok_or("account index out of range")?;
    if let Some(pubkey) = value.as_str() {
        return Ok(Pubkey::from_str(pubkey)?);
    }
    if let Some(pubkey) = value.get("pubkey").and_then(|v| v.as_str()) {
        return Ok(Pubkey::from_str(pubkey)?);
    }
    Err("invalid account key entry".into())
}

fn account_flags(pubkey: &Pubkey, account_keys: &[serde_json::Value]) -> Option<(bool, bool)> {
    account_keys.iter().find_map(|value| {
        let entry_pubkey = value.get("pubkey")?.as_str()?;
        if Pubkey::from_str(entry_pubkey).ok()? != *pubkey {
            return None;
        }
        Some((
            value
                .get("signer")
                .and_then(|v| v.as_bool())
                .unwrap_or(false),
            value
                .get("writable")
                .and_then(|v| v.as_bool())
                .unwrap_or(false),
        ))
    })
}

fn inferred_status_flags(index: usize) -> (bool, bool) {
    match index {
        0 | 1 => (false, true),
        3 => (true, true),
        _ => (false, false),
    }
}

fn decode_instruction_data(
    value: &serde_json::Value,
) -> Result<Vec<u8>, Box<dyn std::error::Error>> {
    if let Some(data) = value.as_str() {
        return Ok(bs58::decode(data).into_vec()?);
    }
    if let Some(values) = value.as_array() {
        let data = values
            .first()
            .and_then(|v| v.as_str())
            .ok_or("invalid data array")?;
        let encoding = values.get(1).and_then(|v| v.as_str()).unwrap_or("base58");
        return match encoding {
            "base64" => Ok(base64::engine::general_purpose::STANDARD.decode(data)?),
            "base58" => Ok(bs58::decode(data).into_vec()?),
            _ => Err(format!("unsupported instruction data encoding {}", encoding).into()),
        };
    }
    Err("unsupported instruction data shape".into())
}

fn decode_image_id(image_id: &str) -> Result<[u8; 32], Box<dyn std::error::Error>> {
    let bytes = hex::decode(image_id)?;
    Ok(bytes
        .try_into()
        .map_err(|_| "image id must decode to 32 bytes")?)
}

fn risc0_sha256(data: &[u8]) -> [u8; 32] {
    let digest = Risc0Sha256Impl::hash_bytes(data);
    let mut out = [0u8; 32];
    out.copy_from_slice(digest.as_bytes());
    out
}

fn solana_sha256(data: &[u8]) -> [u8; 32] {
    hash(data).to_bytes()
}

fn check_hex(
    label: &str,
    actual: &[u8],
    expected_hex: &str,
) -> Result<(), Box<dyn std::error::Error>> {
    let expected = hex::decode(expected_hex)?;
    if actual != expected {
        return Err(format!(
            "{} mismatch: got {}, expected {}",
            label,
            hex::encode(actual),
            expected_hex
        )
        .into());
    }
    Ok(())
}

fn check_pubkey(
    label: &str,
    actual: &[u8],
    expected: &str,
) -> Result<(), Box<dyn std::error::Error>> {
    let expected = Pubkey::from_str(expected)?;
    if actual != expected.as_ref() {
        return Err(format!(
            "{} mismatch: got {}, expected {}",
            label,
            Pubkey::new_from_array(actual.try_into()?),
            expected
        )
        .into());
    }
    Ok(())
}

fn arg_value(args: &[String], key: &str) -> Result<String, Box<dyn std::error::Error>> {
    args.windows(2)
        .find_map(|pair| (pair[0] == key).then(|| pair[1].clone()))
        .ok_or_else(|| format!("missing {}", key).into())
}

#[cfg(test)]
mod tests {
    use super::{framed_input, reducer_committed_outputs, risc0_sha256, solana_hashv, solana_sha256, DEFAULT_INPUT_JSON};

    const SCORE_FELT: &str = "003a000000000000000000000000000000000000000000000000000000000000";
    const LOW_BYTE_FELT: &str = "3901000000000000000000000000000000000000000000000000000000000000";
    const SCORE_OFFSET: usize = 32 + 4 + 4;

    fn outputs_for(score_hex_json: &str) -> Result<Vec<u8>, Box<dyn std::error::Error>> {
        let input = format!(
            "{{\"branch_key\":\"baseline\",\"child_job_id\":\"child-baseline-1\",\"parent_request_id\":\"parent-bonsol-eval\",\"line_count\":3,\"word_count\":17{score_hex_json}}}"
        );
        reducer_committed_outputs(&input)
    }

    #[test]
    fn default_input_matches_golden_vectors() {
        // Golden values computed independently with Python hashlib.
        let outputs = reducer_committed_outputs(DEFAULT_INPUT_JSON).unwrap();
        assert_eq!(outputs.len(), 72);
        assert_eq!(
            hex::encode(&outputs),
            format!("015c09c8aeadb048416fe04d61b50cc187b34eb66e772ea4fff92cdbcf1c2aeb0300000011000000{SCORE_FELT}")
        );
        assert_eq!(
            hex::encode(solana_sha256(&outputs)),
            "76a8ed05cc918de950431cc891b1d316d6d7233b6f9fc951d7e36966e322c1ea"
        );
        let input_digest = risc0_sha256(&framed_input(DEFAULT_INPUT_JSON.as_bytes()));
        assert_eq!(
            hex::encode(input_digest),
            "5ed697e4ca45a8ca9b12f1c439d27f81200bac28ef2b5c404dadb071a2bb2bc4"
        );
        assert_eq!(
            hex::encode(solana_hashv(&[&input_digest, outputs.as_slice()])),
            "c1bb642e1996baa57be6534101ec54e6b43ef19252b1a19c48835f1c8f4c2363"
        );
    }

    #[test]
    fn committed_outputs_layout_is_digest_counts_score_felt() {
        let outputs = outputs_for(&format!(",\"score_hex\":\"{SCORE_FELT}\"")).unwrap();
        assert_eq!(&outputs[32..36], &3u32.to_le_bytes());
        assert_eq!(&outputs[36..40], &17u32.to_le_bytes());
        assert_eq!(&outputs[SCORE_OFFSET..], &hex::decode(SCORE_FELT).unwrap()[..]);
    }

    #[test]
    fn committed_score_carries_the_true_low_byte() {
        let outputs = outputs_for(&format!(",\"score_hex\":\"{LOW_BYTE_FELT}\"")).unwrap();
        assert_eq!(outputs[SCORE_OFFSET], 0x39);
        assert_eq!(outputs[SCORE_OFFSET + 1], 0x01);
        assert_eq!(
            hex::encode(solana_sha256(&outputs)),
            "7f955e6c0e172ab986ac3b3d0a09c9f965204fe3eb8d400db5ab41e1b1ba19f6"
        );
    }

    #[test]
    fn malformed_or_missing_score_hex_is_an_error_not_a_prediction() {
        assert!(outputs_for("").is_err());
        assert!(outputs_for(",\"score_hex\":7").is_err());
        assert!(outputs_for(",\"score_hex\":\"\"").is_err());
        assert!(outputs_for(",\"score_hex\":\"deadbeef\"").is_err());
        assert!(outputs_for(",\"score_hex\":\"zz\"").is_err());
        assert!(outputs_for(",\"score_hex\":\"\u{e9}a\"").is_err());
        assert!(outputs_for(&format!(",\"score_hex\":\"0x{SCORE_FELT}\"")).is_err());
    }
}
