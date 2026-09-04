use mirofish_bonsol_branch_reducer::{committed_outputs, decode_score_felt, reducer_canonical_bytes};
use risc0_zkvm::{
    guest::{env, sha::Impl},
    sha::Sha256,
};

fn main() {
    let mut public_len_bytes = [0u8; 8];
    env::read_slice(&mut public_len_bytes);
    let public_len = u64::from_le_bytes(public_len_bytes) as usize;
    let mut public_bytes = vec![0u8; public_len];
    env::read_slice(&mut public_bytes);
    let framed_input = [&public_len_bytes[..], public_bytes.as_slice()].concat();
    let input_digest = Impl::hash_bytes(&framed_input);
    let public_json = String::from_utf8(public_bytes).unwrap();

    let branch_key = gjson::get(&public_json, "branch_key").str().to_string();
    let child_job_id = gjson::get(&public_json, "child_job_id").str().to_string();
    let parent_request_id = gjson::get(&public_json, "parent_request_id")
        .str()
        .to_string();
    let line_count = gjson::get(&public_json, "line_count").u32();
    let word_count = gjson::get(&public_json, "word_count").u32();
    let score_hex = gjson::get(&public_json, "score_hex").str().to_string();

    // A malformed score_hex aborts the guest, so no receipt exists for it.
    let score = match decode_score_felt(&score_hex) {
        Ok(score) => score,
        Err(error) => panic!("score_hex rejected: {error}"),
    };
    let canonical = reducer_canonical_bytes(
        &branch_key,
        &child_job_id,
        &parent_request_id,
        &score_hex,
        line_count,
        word_count,
    );
    let reducer_digest = Impl::hash_bytes(&canonical);
    let mut reducer_digest_bytes = [0u8; 32];
    reducer_digest_bytes.copy_from_slice(reducer_digest.as_bytes());
    let outputs = committed_outputs(&reducer_digest_bytes, line_count, word_count, &score);

    env::commit_slice(input_digest.as_bytes());
    env::commit_slice(&outputs);
}
