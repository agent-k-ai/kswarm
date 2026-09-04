use std::{env, fs, path::PathBuf};

use base64::{engine::general_purpose::STANDARD as BASE64, Engine as _};
use methods::{METHOD_ELF, METHOD_ID};
use risc0_zkvm::{default_prover, ExecutorEnv, Receipt};
use serde::{Deserialize, Serialize};

#[derive(Debug, Serialize, Deserialize)]
struct ReducerInput {
    branch_key: String,
    child_job_id: String,
    line_count: u32,
    parent_request_id: String,
    score_hex: String,
    word_count: u32,
}

#[derive(Debug, Serialize, Deserialize)]
struct ReducerJournal {
    branch_key: String,
    child_job_id: String,
    line_count: u32,
    parent_request_id: String,
    reducer_digest: String,
    score_hex: String,
    word_count: u32,
}

#[derive(Debug, Serialize, Deserialize)]
struct ReceiptBundle {
    bundle_version: String,
    image_id_hex: String,
    input: ReducerInput,
    journal: ReducerJournal,
    receipt_b64: String,
}

fn main() {
    tracing_subscriber::fmt()
        .with_env_filter(tracing_subscriber::filter::EnvFilter::from_default_env())
        .init();

    let args: Vec<String> = env::args().collect();
    if args.len() < 2 {
        eprintln!("usage: host <prove|verify|image-id> ...");
        std::process::exit(1);
    }

    match args[1].as_str() {
        "image-id" => {
            println!("{}", digest_hex());
        }
        "prove" => {
            if args.len() < 4 {
                eprintln!("usage: host prove <input.json> <output.json>");
                std::process::exit(1);
            }
            let input_path = PathBuf::from(&args[2]);
            let output_path = PathBuf::from(&args[3]);
            prove_bundle(&input_path, &output_path);
        }
        "verify" => {
            if args.len() < 4 {
                eprintln!("usage: host verify <bundle.json> <output.json>");
                std::process::exit(1);
            }
            let bundle_path = PathBuf::from(&args[2]);
            let output_path = PathBuf::from(&args[3]);
            verify_bundle(&bundle_path, &output_path);
        }
        other => {
            eprintln!("unknown command: {other}");
            std::process::exit(1);
        }
    }
}

fn prove_bundle(input_path: &PathBuf, output_path: &PathBuf) {
    let input: ReducerInput = serde_json::from_slice(&fs::read(input_path).unwrap()).unwrap();
    let env = ExecutorEnv::builder().write(&input).unwrap().build().unwrap();
    let receipt = default_prover().prove(env, METHOD_ELF).unwrap().receipt;
    receipt.verify(METHOD_ID).unwrap();

    let journal: ReducerJournal = receipt.journal.decode().unwrap();
    let bundle = ReceiptBundle {
        bundle_version: "kswarm-zkvm-receipt-v1".to_string(),
        image_id_hex: digest_hex(),
        input,
        journal,
        receipt_b64: BASE64.encode(bincode::serialize(&receipt).unwrap()),
    };
    fs::write(output_path, serde_json::to_vec_pretty(&bundle).unwrap()).unwrap();
}

fn verify_bundle(bundle_path: &PathBuf, output_path: &PathBuf) {
    let bundle: ReceiptBundle = serde_json::from_slice(&fs::read(bundle_path).unwrap()).unwrap();
    let receipt: Receipt = bincode::deserialize(&BASE64.decode(bundle.receipt_b64.as_bytes()).unwrap()).unwrap();
    receipt.verify(METHOD_ID).unwrap();
    let journal: ReducerJournal = receipt.journal.decode().unwrap();
    if journal.reducer_digest != bundle.journal.reducer_digest {
        panic!("journal digest mismatch");
    }
    if bundle.image_id_hex != digest_hex() {
        panic!("image id mismatch");
    }
    fs::write(
        output_path,
        serde_json::to_vec_pretty(&serde_json::json!({
            "bundle_version": bundle.bundle_version,
            "image_id_hex": bundle.image_id_hex,
            "journal": journal,
            "verified": true
        }))
        .unwrap(),
    )
    .unwrap();
}

fn digest_hex() -> String {
    METHOD_ID
        .iter()
        .map(|word| format!("{word:08x}"))
        .collect::<Vec<_>>()
        .join("")
}
