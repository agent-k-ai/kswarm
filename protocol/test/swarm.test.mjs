import assert from "node:assert/strict";
import { describe, it } from "node:test";

import {
  BRANCH_KIND,
  PROOF_SYSTEM,
  buildChildReceiptManifest,
  buildParentSwarmRequest,
  expandParentIntoChildJobs
} from "../src/swarm.mjs";

// The planner used to advertise a second, per-branch model-proof lane. Nothing
// ever produced one and no released prover can prove the branch model, so the
// lane is gone. These tests pin that: one proof lane, named once.

function makeParent(overrides = {}) {
  return buildParentSwarmRequest({
    parentRequestId: "parent-1",
    inputCid: "bafy-input",
    inputHash: "0011",
    rewardBudget: 300,
    simulationObjective: "objective",
    branchTemplates: [
      { branchKey: "baseline", scenarioLabel: "Baseline", zkvmProgramDigest: "branch-reducer-v1" },
      { branchKey: "shock", scenarioLabel: "Shock", zkvmProgramDigest: "branch-reducer-v1", deterministicReducer: false }
    ],
    ...overrides
  });
}

describe("PROOF_SYSTEM", () => {
  it("names exactly the lanes that exist", () => {
    assert.deepEqual(Object.keys(PROOF_SYSTEM).sort(), ["none", "zkvm"]);
  });
});

describe("expandParentIntoChildJobs", () => {
  it("gives every execution job a zkvm-only proof requirement", () => {
    const { childJobs, aggregateChildJob } = expandParentIntoChildJobs(makeParent());
    const execution = childJobs.filter((job) => job.kind === BRANCH_KIND.execute);
    assert.equal(execution.length, 2);
    assert.deepEqual(Object.keys(execution[0].proofRequirements), ["zkvm"]);
    assert.deepEqual(execution[0].proofRequirements.zkvm, {
      programDigest: "branch-reducer-v1",
      required: true
    });
    // `deterministicReducer: false` turns the one lane off rather than selecting another.
    assert.deepEqual(execution[1].proofRequirements, { zkvm: null });
    assert.deepEqual(Object.keys(aggregateChildJob.proofRequirements), ["zkvm"]);
  });

  it("carries no model-digest field on a normalized branch", () => {
    const parent = makeParent();
    for (const branch of parent.branches) {
      assert.deepEqual(Object.keys(branch).sort(), [
        "branchKey",
        "deterministicReducer",
        "highValue",
        "requiredStakeMultiplier",
        "scenarioLabel",
        "verificationMode",
        "zkvmProgramDigest"
      ]);
    }
  });
});

describe("buildChildReceiptManifest", () => {
  it("carries one proof descriptor, tagged zkvm", () => {
    const { childJobs } = expandParentIntoChildJobs(makeParent());
    const receipt = buildChildReceiptManifest({
      childJob: childJobs[0],
      executorIdentity: "worker-1",
      inputCid: "bafy-input",
      inputHash: "0011",
      outputCid: "bafy-output",
      outputHash: "2233",
      zkvmReceipt: { journalHash: "j", receiptCid: "bafy-receipt" }
    });
    assert.deepEqual(Object.keys(receipt.proofBundle), ["zkvm"]);
    assert.deepEqual(receipt.proofBundle.zkvm, {
      journalHash: "j",
      receiptCid: "bafy-receipt",
      system: PROOF_SYSTEM.zkvm
    });
  });

  it("records a null descriptor when no receipt is supplied", () => {
    const { childJobs } = expandParentIntoChildJobs(makeParent());
    const receipt = buildChildReceiptManifest({
      childJob: childJobs[0],
      executorIdentity: "worker-1",
      inputCid: "bafy-input",
      inputHash: "0011",
      outputCid: "bafy-output",
      outputHash: "2233"
    });
    assert.deepEqual(receipt.proofBundle, { zkvm: null });
  });
});
