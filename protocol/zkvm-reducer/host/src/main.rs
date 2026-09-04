//! Host for the branch canonicalization guest: `prove`, `verify`, `image-id`.
//!
//! `prove <input.json> <bundle.json>` proves that the MFBR1 document in `input.json`
//! encodes to a particular `MFB2` receipt hash. The bytes of `input.json` are used
//! exactly as they are on disk, because the committed `input_digest` covers them.
//!
//! `verify <bundle.json> <output.json>` verifies the receipt against this binary's own
//! image id, decodes the 68-byte journal, and writes the three committed fields. The
//! caller is expected to bind them to a claim: the Python verifier
//! (`worker/verifier_worker`) checks `input_digest` against the frame it rebuilt from
//! the job's own input and output, `result_hash` against the on-chain
//! `submitted_result_hash`, and `output_len` against the document it fetched.
//!
//! Exit status is the whole contract: any failure exits non-zero with a message on
//! stderr and writes no output file, so a caller cannot mistake a failed verification
//! for a passed one.

use std::{env, fs, path::PathBuf, process};

use base64::{engine::general_purpose::STANDARD as BASE64, Engine as _};
use kswarm_bonsol_aggregate_reducer::branch_receipt::BRANCH_RECEIPT_JOURNAL_LEN;
use methods::{METHOD_ELF, METHOD_ID};
use risc0_zkvm::{default_prover, ExecutorEnv, Receipt};
use serde::{Deserialize, Serialize};

const BUNDLE_VERSION: &str = "kswarm-branch-receipt-v1";

#[derive(Debug, Serialize, Deserialize)]
struct ReceiptJournal {
    input_digest: String,
    result_hash: String,
    output_len: u32,
}

#[derive(Debug, Serialize, Deserialize)]
struct ReceiptBundle {
    bundle_version: String,
    image_id_hex: String,
    journal: ReceiptJournal,
    journal_hex: String,
    receipt_b64: String,
}

fn main() {
    tracing_subscriber::fmt()
        .with_env_filter(tracing_subscriber::filter::EnvFilter::from_default_env())
        .init();

    let args: Vec<String> = env::args().collect();
    let result = match args.get(1).map(String::as_str) {
        Some("image-id") => {
            println!("{}", digest_hex());
            Ok(())
        }
        Some("prove") if args.len() >= 4 => prove_bundle(&PathBuf::from(&args[2]), &PathBuf::from(&args[3])),
        Some("verify") if args.len() >= 4 => verify_bundle(&PathBuf::from(&args[2]), &PathBuf::from(&args[3])),
        Some("prove") | Some("verify") => Err("usage: host <prove|verify> <in.json> <out.json>".to_string()),
        other => Err(format!(
            "usage: host <prove|verify|image-id> ...; got {:?}",
            other.unwrap_or("")
        )),
    };
    if let Err(message) = result {
        eprintln!("{message}");
        process::exit(1);
    }
}

fn prove_bundle(input_path: &PathBuf, output_path: &PathBuf) -> Result<(), String> {
    let payload = fs::read(input_path).map_err(|error| format!("cannot read {}: {error}", input_path.display()))?;
    let env = ExecutorEnv::builder()
        .write(&payload)
        .map_err(|error| format!("cannot write the guest input: {error}"))?
        .build()
        .map_err(|error| format!("cannot build the executor environment: {error}"))?;
    let receipt = default_prover()
        .prove(env, METHOD_ELF)
        .map_err(|error| format!("prove failed: {error}"))?
        .receipt;
    receipt
        .verify(METHOD_ID)
        .map_err(|error| format!("the receipt this host just produced does not verify: {error}"))?;
    let bundle = bundle_from(&receipt)?;
    fs::write(
        output_path,
        serde_json::to_vec_pretty(&bundle).map_err(|error| format!("cannot encode the bundle: {error}"))?,
    )
    .map_err(|error| format!("cannot write {}: {error}", output_path.display()))?;
    Ok(())
}

fn verify_bundle(bundle_path: &PathBuf, output_path: &PathBuf) -> Result<(), String> {
    let raw = fs::read(bundle_path).map_err(|error| format!("cannot read {}: {error}", bundle_path.display()))?;
    let bundle: ReceiptBundle =
        serde_json::from_slice(&raw).map_err(|error| format!("{} is not a receipt bundle: {error}", bundle_path.display()))?;
    if bundle.bundle_version != BUNDLE_VERSION {
        return Err(format!(
            "unknown bundle version {:?}; this host writes {BUNDLE_VERSION}",
            bundle.bundle_version
        ));
    }
    // The image id is checked before the receipt, so a bundle that names another guest
    // is refused rather than verified against this one and reported as passing.
    if bundle.image_id_hex != digest_hex() {
        return Err(format!(
            "bundle image id {} is not this binary's image id {}",
            bundle.image_id_hex,
            digest_hex()
        ));
    }
    let receipt_bytes = BASE64
        .decode(bundle.receipt_b64.as_bytes())
        .map_err(|error| format!("receipt_b64 is not base64: {error}"))?;
    let receipt: Receipt =
        bincode::deserialize(&receipt_bytes).map_err(|error| format!("receipt_b64 is not a receipt: {error}"))?;
    receipt
        .verify(METHOD_ID)
        .map_err(|error| format!("receipt does not verify: {error}"))?;
    let verified = bundle_from(&receipt)?;
    if verified.journal_hex != bundle.journal_hex {
        return Err(format!(
            "the verified journal {} is not the journal the bundle claims {}",
            verified.journal_hex, bundle.journal_hex
        ));
    }
    fs::write(
        output_path,
        serde_json::to_vec_pretty(&serde_json::json!({
            "bundle_version": BUNDLE_VERSION,
            "image_id_hex": verified.image_id_hex,
            "journal": verified.journal,
            "journal_hex": verified.journal_hex,
            "verified": true,
        }))
        .map_err(|error| format!("cannot encode the result: {error}"))?,
    )
    .map_err(|error| format!("cannot write {}: {error}", output_path.display()))?;
    Ok(())
}

fn bundle_from(receipt: &Receipt) -> Result<ReceiptBundle, String> {
    let journal = receipt.journal.bytes.clone();
    if journal.len() != BRANCH_RECEIPT_JOURNAL_LEN {
        return Err(format!(
            "journal is {} bytes; this guest commits {BRANCH_RECEIPT_JOURNAL_LEN}",
            journal.len()
        ));
    }
    Ok(ReceiptBundle {
        bundle_version: BUNDLE_VERSION.to_string(),
        image_id_hex: digest_hex(),
        journal: ReceiptJournal {
            input_digest: hex(&journal[..32]),
            result_hash: hex(&journal[32..64]),
            output_len: u32::from_le_bytes([journal[64], journal[65], journal[66], journal[67]]),
        },
        journal_hex: hex(&journal),
        receipt_b64: BASE64.encode(
            bincode::serialize(receipt).map_err(|error| format!("cannot serialize the receipt: {error}"))?,
        ),
    })
}

fn hex(bytes: &[u8]) -> String {
    bytes.iter().map(|byte| format!("{byte:02x}")).collect()
}

fn digest_hex() -> String {
    METHOD_ID
        .iter()
        .map(|word| format!("{word:08x}"))
        .collect::<Vec<_>>()
        .join("")
}
