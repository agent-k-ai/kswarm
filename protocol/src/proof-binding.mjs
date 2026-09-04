// Pure binding checks between a branch output manifest and its proof artifacts.
//
// A proof only covers the values it was made from. These checks tie those
// values to the result the manifest claims. Every check fails closed: a
// missing field, a wrong type, or a mismatch throws ProofBindingError.
//
// This module has no I/O and no dependency on the ezkl or zkvm binaries so
// it can run under `node --test` without node_modules.

import { createHash } from "crypto";

export const BRANCH_OUTPUT_MANIFEST_VERSION = "kswarm-branch-output-v1";
export const EZKL_BUNDLE_VERSION = "kswarm-ezkl-proof-v1";
export const ZKVM_BUNDLE_VERSION = "kswarm-zkvm-receipt-v1";

// Journal fields committed by protocol/zkvm-reducer/methods/guest/src/main.rs.
export const ZKVM_JOURNAL_FIELDS = Object.freeze([
  "branch_key",
  "child_job_id",
  "line_count",
  "parent_request_id",
  "reducer_digest",
  "score_hex",
  "word_count"
]);

const FELT_HEX_PATTERN = /^[0-9a-f]{64}$/;
const SHA256_HEX_PATTERN = /^[0-9a-f]{64}$/;
const U32_MAX = 0xffffffff;
// BN254 scalar field modulus r, little-endian bytes.
const BN254_SCALAR_MODULUS_LE = Buffer.from("010000f093f5e1439170b97948e833285d588181b64550b829a031e1724e6430", "hex");
export const SCORE_FELT_LENGTH = 32;
// reducer_digest (32) || line_count le32 || word_count le32 || score (32)
export const BONSOL_COMMITTED_OUTPUTS_LENGTH = 32 + 4 + 4 + SCORE_FELT_LENGTH;

export class ProofBindingError extends Error {
  constructor(message) {
    super(message);
    this.name = "ProofBindingError";
  }
}

function isObject(value) {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function requireObject(value, label) {
  if (!isObject(value)) {
    throw new ProofBindingError(`${label} must be an object`);
  }
  return value;
}

function requireString(value, label) {
  if (typeof value !== "string") {
    throw new ProofBindingError(`${label} must be a string`);
  }
  return value;
}

function requireU32(value, label) {
  if (!Number.isInteger(value) || value < 0 || value > U32_MAX) {
    throw new ProofBindingError(`${label} must be an integer in [0, 2^32)`);
  }
  return value;
}

function requireHex64(value, label, pattern) {
  requireString(value, label);
  if (!pattern.test(value)) {
    throw new ProofBindingError(`${label} must be 64 lowercase hex characters`);
  }
  return value;
}

function isReducedLittleEndian(bytes) {
  for (let index = SCORE_FELT_LENGTH - 1; index >= 0; index -= 1) {
    if (bytes[index] !== BN254_SCALAR_MODULUS_LE[index]) {
      return bytes[index] < BN254_SCALAR_MODULUS_LE[index];
    }
  }
  return false;
}

// Decode a score_hex (64 lowercase hex digits, little-endian, reduced) into
// the 32 bytes the Bonsol guest commits. bytes[0] is the least significant.
export function decodeScoreFelt(scoreHex, label = "score_hex") {
  requireHex64(scoreHex, label, FELT_HEX_PATTERN);
  const bytes = Buffer.from(scoreHex, "hex");
  if (!isReducedLittleEndian(bytes)) {
    throw new ProofBindingError(`${label} is not reduced modulo the BN254 scalar field`);
  }
  return bytes;
}

function requireEqual(actual, expected, label) {
  if (actual !== expected) {
    throw new ProofBindingError(`${label} mismatch`);
  }
}

function u32LittleEndian(value) {
  const bytes = Buffer.alloc(4);
  bytes.writeUInt32LE(value, 0);
  return bytes;
}

// Mirrors the hash the off-chain zkVM guest commits as `reducer_digest`.
export function computeReducerDigest({ branch_key, child_job_id, parent_request_id, score_hex, line_count, word_count }) {
  const hasher = createHash("sha256");
  hasher.update(Buffer.from(requireString(branch_key, "branch_key"), "utf8"));
  hasher.update(Buffer.from(requireString(child_job_id, "child_job_id"), "utf8"));
  hasher.update(Buffer.from(requireString(parent_request_id, "parent_request_id"), "utf8"));
  hasher.update(Buffer.from(requireString(score_hex, "score_hex"), "utf8"));
  hasher.update(u32LittleEndian(requireU32(line_count, "line_count")));
  hasher.update(u32LittleEndian(requireU32(word_count, "word_count")));
  return hasher.digest("hex");
}

// Extract and type-check the values every proof must bind to.
export function manifestClaim(manifest) {
  requireObject(manifest, "manifest");
  requireEqual(manifest.bundle_version, BRANCH_OUTPUT_MANIFEST_VERSION, "manifest.bundle_version");
  const result = requireObject(manifest.result, "manifest.result");
  const claim = {
    branchKey: requireString(result.branch_key, "manifest.result.branch_key"),
    childJobId: requireString(result.child_job_id, "manifest.result.child_job_id"),
    parentRequestId: requireString(result.parent_request_id, "manifest.result.parent_request_id"),
    lineCount: requireU32(result.line_count, "manifest.result.line_count"),
    wordCount: requireU32(result.word_count, "manifest.result.word_count")
  };
  requireEqual(manifest.branch_key, claim.branchKey, "manifest.branch_key");
  requireEqual(manifest.child_job_id, claim.childJobId, "manifest.child_job_id");
  requireEqual(manifest.parent_request_id, claim.parentRequestId, "manifest.parent_request_id");
  const proofs = requireObject(manifest.proofs, "manifest.proofs");
  const ezkl = requireObject(proofs.ezkl, "manifest.proofs.ezkl");
  decodeScoreFelt(ezkl.score_hex, "manifest.proofs.ezkl.score_hex");
  claim.scoreHex = ezkl.score_hex;
  return claim;
}

// The EZKL bundle is the prover's claim about the proof instances. It must
// state the same values the manifest states. verify_branch.py then binds the
// proof instances to these values.
export function bindEzklBundleToManifest({ manifest, ezklBundle }) {
  const claim = manifestClaim(manifest);
  const ezkl = manifest.proofs.ezkl;
  const bundle = requireObject(ezklBundle, "ezkl bundle");
  requireEqual(bundle.bundle_version, EZKL_BUNDLE_VERSION, "ezkl bundle.bundle_version");
  const features = requireObject(bundle.features, "ezkl bundle.features");
  if (typeof features.line_count !== "number" || features.line_count !== claim.lineCount) {
    throw new ProofBindingError("ezkl bundle.features.line_count mismatch");
  }
  if (typeof features.word_count !== "number" || features.word_count !== claim.wordCount) {
    throw new ProofBindingError("ezkl bundle.features.word_count mismatch");
  }
  requireEqual(bundle.score_hex, claim.scoreHex, "ezkl bundle.score_hex");
  const instances = bundle.public_instances;
  if (!Array.isArray(instances) || instances.length !== 1 || !Array.isArray(instances[0]) || instances[0].length === 0) {
    throw new ProofBindingError("ezkl bundle.public_instances must hold one non-empty column");
  }
  requireEqual(instances[0][instances[0].length - 1], claim.scoreHex, "ezkl bundle.public_instances output");
  requireEqual(bundle.proof_sha256, requireHex64(ezkl.proof_sha256, "manifest.proofs.ezkl.proof_sha256", SHA256_HEX_PATTERN), "ezkl bundle.proof_sha256");
  requireEqual(bundle.vk_sha256, requireHex64(ezkl.vk_sha256, "manifest.proofs.ezkl.vk_sha256", SHA256_HEX_PATTERN), "ezkl bundle.vk_sha256");
  return claim;
}

function checkJournalShape(journal, label) {
  requireObject(journal, label);
  const extra = Object.keys(journal).filter((key) => !ZKVM_JOURNAL_FIELDS.includes(key));
  if (extra.length > 0) {
    throw new ProofBindingError(`${label} has unexpected fields: ${extra.sort().join(", ")}`);
  }
  requireString(journal.branch_key, `${label}.branch_key`);
  requireString(journal.child_job_id, `${label}.child_job_id`);
  requireString(journal.parent_request_id, `${label}.parent_request_id`);
  requireU32(journal.line_count, `${label}.line_count`);
  requireU32(journal.word_count, `${label}.word_count`);
  decodeScoreFelt(journal.score_hex, `${label}.score_hex`);
  requireHex64(journal.reducer_digest, `${label}.reducer_digest`, SHA256_HEX_PATTERN);
  return journal;
}

// `verification` is the JSON written by `zkvm-reducer verify`. The receipt
// was already checked against the verifier's own image id. This binds every
// committed journal field to the manifest claim.
export function bindZkvmJournalToManifest({ manifest, verification }) {
  const claim = manifestClaim(manifest);
  const zkvm = requireObject(manifest.proofs.zkvm, "manifest.proofs.zkvm");
  const verified = requireObject(verification, "zkvm verification");
  if (verified.verified !== true) {
    throw new ProofBindingError("zkvm verification.verified must be true");
  }
  requireEqual(verified.bundle_version, ZKVM_BUNDLE_VERSION, "zkvm verification.bundle_version");
  requireHex64(verified.image_id_hex, "zkvm verification.image_id_hex", SHA256_HEX_PATTERN);
  requireEqual(zkvm.image_id_hex, verified.image_id_hex, "zkvm image id");

  const journal = checkJournalShape(verified.journal, "zkvm verification.journal");
  requireEqual(journal.branch_key, claim.branchKey, "zkvm journal.branch_key");
  requireEqual(journal.child_job_id, claim.childJobId, "zkvm journal.child_job_id");
  requireEqual(journal.parent_request_id, claim.parentRequestId, "zkvm journal.parent_request_id");
  requireEqual(journal.line_count, claim.lineCount, "zkvm journal.line_count");
  requireEqual(journal.word_count, claim.wordCount, "zkvm journal.word_count");
  requireEqual(journal.score_hex, claim.scoreHex, "zkvm journal.score_hex");
  requireEqual(journal.reducer_digest, computeReducerDigest(journal), "zkvm journal.reducer_digest");

  const recorded = checkJournalShape(zkvm.journal, "manifest.proofs.zkvm.journal");
  for (const field of ZKVM_JOURNAL_FIELDS) {
    requireEqual(recorded[field], journal[field], `manifest.proofs.zkvm.journal.${field}`);
  }
  return { claim, journal };
}

export function bindBranchManifest({ manifest, ezklBundle, zkvmVerification }) {
  const claim = bindEzklBundleToManifest({ manifest, ezklBundle });
  const { journal } = bindZkvmJournalToManifest({ manifest, verification: zkvmVerification });
  return { claim, journal };
}


// --- Bonsol reducer journal contract ------------------------------------
//
// Mirrors protocol/bonsol-branch-reducer/src/lib.rs. The guest journal is
// `input_digest (32) || committed_outputs (72)`. Bonsol forwards the committed
// outputs to the callback; the on-chain program hashes them and never parses
// them. Every predictor of the journal hash must use this exact layout.

export function bonsolReducerCanonicalBytes({ branch_key, child_job_id, parent_request_id, score_hex, line_count, word_count }) {
  const parts = [
    requireString(branch_key, "branch_key"),
    requireString(child_job_id, "child_job_id"),
    requireString(parent_request_id, "parent_request_id"),
    requireString(score_hex, "score_hex"),
    String(requireU32(line_count, "line_count")),
    String(requireU32(word_count, "word_count"))
  ];
  return Buffer.from(parts.join("|"), "utf8");
}

export function bonsolCommittedOutputs(fields) {
  const score = decodeScoreFelt(fields?.score_hex);
  const canonical = bonsolReducerCanonicalBytes(fields);
  const reducerDigest = createHash("sha256").update(canonical).digest();
  const outputs = Buffer.concat([reducerDigest, u32LittleEndian(fields.line_count), u32LittleEndian(fields.word_count), score]);
  if (outputs.length !== BONSOL_COMMITTED_OUTPUTS_LENGTH) {
    throw new ProofBindingError("committed outputs have the wrong length");
  }
  return outputs;
}

// Bonsol public input framing: `len le64 || payload`. Its SHA-256 is the input digest.
export function bonsolFramedInput(payload) {
  if (!Buffer.isBuffer(payload)) {
    throw new ProofBindingError("payload must be a Buffer");
  }
  const length = Buffer.alloc(8);
  length.writeBigUInt64LE(BigInt(payload.length), 0);
  return Buffer.concat([length, payload]);
}

// The hash the on-chain program stores: sha256(input_digest || committed_outputs).
export function bonsolJournalHash(inputDigest, committedOutputs) {
  if (!Buffer.isBuffer(inputDigest) || inputDigest.length !== 32) {
    throw new ProofBindingError("input_digest must be 32 bytes");
  }
  if (!Buffer.isBuffer(committedOutputs) || committedOutputs.length !== BONSOL_COMMITTED_OUTPUTS_LENGTH) {
    throw new ProofBindingError(`committed_outputs must be ${BONSOL_COMMITTED_OUTPUTS_LENGTH} bytes`);
  }
  return createHash("sha256").update(inputDigest).update(committedOutputs).digest("hex");
}
