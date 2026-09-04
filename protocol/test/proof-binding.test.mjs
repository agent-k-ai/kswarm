import assert from "node:assert/strict";
import { describe, it } from "node:test";

import {
  ProofBindingError,
  ZKVM_JOURNAL_FIELDS,
  bindZkvmJournalToManifest,
  computeReducerDigest,
  manifestClaim
} from "../src/proof-binding.mjs";

// line_count=3, word_count=17, score=58 encoded as little-endian BN254 field elements.
const SCORE_FELT = "003a000000000000000000000000000000000000000000000000000000000000";
const OTHER_FELT = "003b000000000000000000000000000000000000000000000000000000000000";
const IMAGE_ID = "a1".repeat(32);
// sha256("baseline" || "child-baseline-1" || "parent-bonsol-eval" || SCORE_FELT || le32(3) || le32(17)),
// computed independently with Python hashlib.
const GOLDEN_DIGEST = "12174ca05c18445649001287ee854c609b1d1a8b131d81ba89d214d147a90f35";

function makeJournal(overrides = {}) {
  const journal = {
    branch_key: "baseline",
    child_job_id: "child-baseline-1",
    line_count: 3,
    parent_request_id: "parent-bonsol-eval",
    reducer_digest: GOLDEN_DIGEST,
    score_hex: SCORE_FELT,
    word_count: 17,
    ...overrides
  };
  return journal;
}

function makeVerification(overrides = {}) {
  return {
    bundle_version: "kswarm-zkvm-receipt-v1",
    image_id_hex: IMAGE_ID,
    journal: makeJournal(),
    verified: true,
    ...overrides
  };
}

function makeManifest(overrides = {}) {
  return {
    bundle_version: "kswarm-branch-output-v1",
    child_job_id: "child-baseline-1",
    branch_key: "baseline",
    parent_request_id: "parent-bonsol-eval",
    result: {
      branch_key: "baseline",
      child_job_id: "child-baseline-1",
      byte_length: 42,
      executor_version: "swarm-child-v1",
      parent_request_id: "parent-bonsol-eval",
      line_count: 3,
      score_hex: SCORE_FELT,
      word_count: 17
    },
    proofs: {
      zkvm: {
        bundle_sha256: "cd".repeat(32),
        image_id_hex: IMAGE_ID,
        journal: makeJournal(),
        verified: true
      }
    },
    artifacts: {
      zkvm_bundle_cid: "bafy-zkvm-bundle"
    },
    ...overrides
  };
}

function withResult(patch) {
  const manifest = makeManifest();
  Object.assign(manifest.result, patch);
  return manifest;
}

function rejects(fn, pattern) {
  assert.throws(fn, (error) => error instanceof ProofBindingError && pattern.test(error.message), `expected ProofBindingError matching ${pattern}`);
}

describe("computeReducerDigest", () => {
  it("matches the guest hash for the golden vector", () => {
    assert.equal(computeReducerDigest(makeJournal()), GOLDEN_DIGEST);
  });

  it("changes when any committed field changes", () => {
    const base = computeReducerDigest(makeJournal());
    for (const patch of [
      { branch_key: "optimistic" },
      { child_job_id: "child-baseline-2" },
      { parent_request_id: "other" },
      { score_hex: OTHER_FELT },
      { line_count: 4 },
      { word_count: 16 }
    ]) {
      assert.notEqual(computeReducerDigest(makeJournal(patch)), base, JSON.stringify(patch));
    }
  });

  it("rejects non-integer counts and non-string ids", () => {
    rejects(() => computeReducerDigest(makeJournal({ line_count: 3.5 })), /line_count/);
    rejects(() => computeReducerDigest(makeJournal({ word_count: -1 })), /word_count/);
    rejects(() => computeReducerDigest(makeJournal({ word_count: 2 ** 32 })), /word_count/);
    rejects(() => computeReducerDigest(makeJournal({ branch_key: 7 })), /branch_key/);
  });
});

describe("manifestClaim", () => {
  it("extracts the claim", () => {
    assert.deepEqual(manifestClaim(makeManifest()), {
      branchKey: "baseline",
      childJobId: "child-baseline-1",
      parentRequestId: "parent-bonsol-eval",
      lineCount: 3,
      wordCount: 17,
      scoreHex: SCORE_FELT
    });
  });

  it("fails closed on a wrong manifest version", () => {
    rejects(() => manifestClaim(makeManifest({ bundle_version: "kswarm-branch-output-v0" })), /bundle_version/);
  });

  it("fails closed when top-level ids disagree with result ids", () => {
    rejects(() => manifestClaim(makeManifest({ branch_key: "optimistic" })), /manifest.branch_key/);
    rejects(() => manifestClaim(makeManifest({ child_job_id: "x" })), /manifest.child_job_id/);
    rejects(() => manifestClaim(makeManifest({ parent_request_id: "x" })), /manifest.parent_request_id/);
  });

  it("fails closed on missing or mistyped result fields", () => {
    rejects(() => manifestClaim(makeManifest({ result: undefined })), /manifest.result/);
    rejects(() => manifestClaim(withResult({ line_count: "3" })), /line_count/);
    rejects(() => manifestClaim(withResult({ word_count: 1.5 })), /word_count/);
    rejects(() => manifestClaim(withResult({ branch_key: null })), /branch_key/);
  });

  it("fails closed on a malformed score_hex", () => {
    const manifest = makeManifest();
    manifest.result.score_hex = "0x3a00";
    rejects(() => manifestClaim(manifest), /score_hex/);
    manifest.result.score_hex = SCORE_FELT.toUpperCase();
    rejects(() => manifestClaim(manifest), /score_hex/);
    delete manifest.result.score_hex;
    rejects(() => manifestClaim(manifest), /score_hex/);
  });

  it("fails closed when the manifest carries no proofs section", () => {
    const manifest = makeManifest();
    delete manifest.proofs;
    rejects(() => manifestClaim(manifest), /manifest.proofs/);
  });
});

describe("bindZkvmJournalToManifest", () => {
  it("passes when every journal field matches the manifest", () => {
    const { journal } = bindZkvmJournalToManifest({ manifest: makeManifest(), verification: makeVerification() });
    assert.deepEqual(journal, makeJournal());
  });

  it("fails on each single-field journal mismatch", () => {
    const cases = [
      [{ branch_key: "optimistic" }, /branch_key/],
      [{ child_job_id: "child-baseline-2" }, /child_job_id/],
      [{ parent_request_id: "other" }, /parent_request_id/],
      [{ line_count: 4 }, /line_count/],
      [{ word_count: 16 }, /word_count/],
      [{ score_hex: OTHER_FELT }, /score_hex/],
      [{ reducer_digest: "00".repeat(32) }, /reducer_digest/]
    ];
    for (const [patch, pattern] of cases) {
      const verification = makeVerification({ journal: makeJournal(patch) });
      rejects(() => bindZkvmJournalToManifest({ manifest: makeManifest(), verification }), pattern);
    }
  });

  it("fails when the journal digest was recomputed for other values", () => {
    const patched = makeJournal({ line_count: 4 });
    patched.reducer_digest = computeReducerDigest(patched);
    const verification = makeVerification({ journal: patched });
    rejects(() => bindZkvmJournalToManifest({ manifest: makeManifest(), verification }), /line_count/);
  });

  it("fails when the manifest recorded journal differs from the verified journal", () => {
    for (const field of ZKVM_JOURNAL_FIELDS) {
      const manifest = makeManifest();
      const changed = field === "line_count" || field === "word_count" ? 99 : field === "reducer_digest" ? "ff".repeat(32) : field === "score_hex" ? OTHER_FELT : "changed";
      manifest.proofs.zkvm.journal[field] = changed;
      rejects(() => bindZkvmJournalToManifest({ manifest, verification: makeVerification() }), new RegExp(`journal.${field}`));
    }
  });

  it("fails on image id mismatch or malformed image id", () => {
    rejects(() => bindZkvmJournalToManifest({ manifest: makeManifest(), verification: makeVerification({ image_id_hex: "b2".repeat(32) }) }), /image id/);
    rejects(() => bindZkvmJournalToManifest({ manifest: makeManifest(), verification: makeVerification({ image_id_hex: "b2" }) }), /image_id_hex/);
  });

  it("fails when verification did not verify or has the wrong version", () => {
    rejects(() => bindZkvmJournalToManifest({ manifest: makeManifest(), verification: makeVerification({ verified: false }) }), /verified/);
    rejects(() => bindZkvmJournalToManifest({ manifest: makeManifest(), verification: makeVerification({ verified: "true" }) }), /verified/);
    rejects(() => bindZkvmJournalToManifest({ manifest: makeManifest(), verification: makeVerification({ bundle_version: "kswarm-zkvm-receipt-v0" }) }), /bundle_version/);
  });

  it("fails on missing, extra, or mistyped journal fields", () => {
    for (const field of ZKVM_JOURNAL_FIELDS) {
      const journal = makeJournal();
      delete journal[field];
      rejects(() => bindZkvmJournalToManifest({ manifest: makeManifest(), verification: makeVerification({ journal }) }), new RegExp(field));
    }
    rejects(() => bindZkvmJournalToManifest({ manifest: makeManifest(), verification: makeVerification({ journal: makeJournal({ extra: 1 }) }) }), /unexpected fields: extra/);
    rejects(() => bindZkvmJournalToManifest({ manifest: makeManifest(), verification: makeVerification({ journal: makeJournal({ line_count: "3" }) }) }), /line_count/);
    rejects(() => bindZkvmJournalToManifest({ manifest: makeManifest(), verification: makeVerification({ journal: null }) }), /journal/);
    rejects(() => bindZkvmJournalToManifest({ manifest: makeManifest(), verification: null }), /verification/);
  });

  it("fails when the manifest has no zkvm section", () => {
    const manifest = makeManifest();
    delete manifest.proofs.zkvm;
    rejects(() => bindZkvmJournalToManifest({ manifest, verification: makeVerification() }), /proofs.zkvm/);
  });
});
