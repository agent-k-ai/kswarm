import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { describe, it } from "node:test";

import {
  BONSOL_COMMITTED_OUTPUTS_LENGTH,
  ProofBindingError,
  bonsolCommittedOutputs,
  bonsolFramedInput,
  bonsolJournalHash,
  bonsolReducerCanonicalBytes,
  decodeScoreFelt,
  manifestClaim
} from "../src/proof-binding.mjs";

const SCORE_FELT = "003a000000000000000000000000000000000000000000000000000000000000";
const LOW_BYTE_FELT = "3901000000000000000000000000000000000000000000000000000000000000";
const MINUS_ONE_FELT = "000000f093f5e1439170b97948e833285d588181b64550b829a031e1724e6430";
const MODULUS_FELT = "010000f093f5e1439170b97948e833285d588181b64550b829a031e1724e6430";
const DEFAULT_INPUT_JSON =
  '{"branch_key":"baseline","child_job_id":"child-baseline-1","parent_request_id":"parent-bonsol-eval","line_count":3,"word_count":17,"score_hex":"' +
  SCORE_FELT +
  '"}';
const CLAIM = {
  branch_key: "baseline",
  child_job_id: "child-baseline-1",
  parent_request_id: "parent-bonsol-eval",
  score_hex: SCORE_FELT,
  line_count: 3,
  word_count: 17
};

function rejects(fn, pattern) {
  assert.throws(fn, (error) => error instanceof ProofBindingError && pattern.test(error.message), `expected ProofBindingError matching ${pattern}`);
}

describe("decodeScoreFelt", () => {
  it("returns little-endian bytes in string order", () => {
    assert.deepEqual([...decodeScoreFelt(SCORE_FELT).subarray(0, 2)], [0x00, 0x3a]);
    assert.deepEqual([...decodeScoreFelt(LOW_BYTE_FELT).subarray(0, 2)], [0x39, 0x01]);
    assert.equal(decodeScoreFelt(LOW_BYTE_FELT).readUInt16LE(0), 313);
  });

  it("regression: the low byte is the first byte, not the last hex pair", () => {
    assert.equal(parseInt(LOW_BYTE_FELT.slice(-2), 16), 0);
    assert.equal(decodeScoreFelt(LOW_BYTE_FELT)[0], 0x39);
  });

  it("accepts r-1 and rejects r and above", () => {
    assert.equal(decodeScoreFelt(MINUS_ONE_FELT).length, 32);
    rejects(() => decodeScoreFelt(MODULUS_FELT), /not reduced/);
    rejects(() => decodeScoreFelt("ff".repeat(32)), /not reduced/);
  });

  it("rejects malformed strings", () => {
    for (const bad of ["", "a", "zz", "0xff", "deadbeef", SCORE_FELT.toUpperCase(), "0x" + SCORE_FELT, 7, null]) {
      rejects(() => decodeScoreFelt(bad), /score_hex/);
    }
  });
});

describe("manifestClaim score_hex", () => {
  it("rejects an unreduced score_hex", () => {
    const manifest = {
      bundle_version: "kswarm-branch-output-v1",
      branch_key: "b",
      child_job_id: "c",
      parent_request_id: "p",
      result: { branch_key: "b", child_job_id: "c", parent_request_id: "p", line_count: 1, word_count: 2 },
      proofs: { ezkl: { score_hex: MODULUS_FELT } }
    };
    rejects(() => manifestClaim(manifest), /not reduced/);
  });
});

describe("bonsolCommittedOutputs", () => {
  it("matches the golden vector shared with the Rust and Python predictors", () => {
    const outputs = bonsolCommittedOutputs(CLAIM);
    assert.equal(outputs.length, BONSOL_COMMITTED_OUTPUTS_LENGTH);
    assert.equal(outputs.length, 72);
    assert.equal(outputs.toString("hex"), "015c09c8aeadb048416fe04d61b50cc187b34eb66e772ea4fff92cdbcf1c2aeb0300000011000000" + SCORE_FELT);
    assert.equal(createHash("sha256").update(outputs).digest("hex"), "76a8ed05cc918de950431cc891b1d316d6d7233b6f9fc951d7e36966e322c1ea");
    assert.equal(bonsolReducerCanonicalBytes(CLAIM).toString("utf8"), `baseline|child-baseline-1|parent-bonsol-eval|${SCORE_FELT}|3|17`);
  });

  it("carries the true low byte", () => {
    const outputs = bonsolCommittedOutputs({ ...CLAIM, score_hex: LOW_BYTE_FELT });
    assert.equal(outputs[40], 0x39);
    assert.equal(outputs[41], 0x01);
    assert.equal(createHash("sha256").update(outputs).digest("hex"), "7f955e6c0e172ab986ac3b3d0a09c9f965204fe3eb8d400db5ab41e1b1ba19f6");
  });

  it("rejects bad fields", () => {
    rejects(() => bonsolCommittedOutputs({ ...CLAIM, line_count: -1 }), /line_count/);
    rejects(() => bonsolCommittedOutputs({ ...CLAIM, word_count: 2 ** 32 }), /word_count/);
    rejects(() => bonsolCommittedOutputs({ ...CLAIM, branch_key: 1 }), /branch_key/);
    rejects(() => bonsolCommittedOutputs({ ...CLAIM, score_hex: "deadbeef" }), /score_hex/);
    rejects(() => bonsolCommittedOutputs(null), /score_hex/);
  });
});

describe("bonsolJournalHash", () => {
  it("matches the golden vector for the harness default input", () => {
    const framed = bonsolFramedInput(Buffer.from(DEFAULT_INPUT_JSON, "utf8"));
    assert.equal(framed.readBigUInt64LE(0), BigInt(DEFAULT_INPUT_JSON.length));
    const inputDigest = createHash("sha256").update(framed).digest();
    assert.equal(inputDigest.toString("hex"), "5ed697e4ca45a8ca9b12f1c439d27f81200bac28ef2b5c404dadb071a2bb2bc4");
    assert.equal(bonsolJournalHash(inputDigest, bonsolCommittedOutputs(CLAIM)), "c1bb642e1996baa57be6534101ec54e6b43ef19252b1a19c48835f1c8f4c2363");
  });

  it("rejects wrong sizes", () => {
    const outputs = bonsolCommittedOutputs(CLAIM);
    rejects(() => bonsolJournalHash(Buffer.alloc(31), outputs), /input_digest/);
    rejects(() => bonsolJournalHash(Buffer.alloc(32), outputs.subarray(1)), /committed_outputs/);
    rejects(() => bonsolFramedInput("text"), /Buffer/);
  });
});
