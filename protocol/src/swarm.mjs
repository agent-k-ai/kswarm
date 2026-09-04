import { canonicalJson, sha256Hex } from "./common.mjs";

export const BRANCH_KIND = {
  aggregate: "aggregate",
  execute: "execute",
  verify: "verify"
};

export const PROOF_SYSTEM = {
  ezkl: "ezkl",
  none: "none",
  zkvm: "zkvm"
};

export function buildParentSwarmRequest({
  aggregateZkvmProgramDigest = null,
  branchPlanName = "default-branch-plan-v1",
  branchTemplates,
  inputCid,
  inputHash,
  parentRequestId,
  rewardBudget,
  simulationObjective,
  verificationPolicy = "proofs-plus-challenge"
}) {
  if (!parentRequestId) {
    throw new Error("parentRequestId is required");
  }
  if (!inputCid) {
    throw new Error("inputCid is required");
  }
  if (!inputHash) {
    throw new Error("inputHash is required");
  }
  if (!Array.isArray(branchTemplates) || branchTemplates.length === 0) {
    throw new Error("branchTemplates must contain at least one branch");
  }

  const normalizedBranches = branchTemplates.map(normalizeBranchTemplate);
  const branchPlanHash = sha256Hex(Buffer.from(canonicalJson({
    branchPlanName,
    branches: normalizedBranches
  })));

  return {
    aggregateZkvmProgramDigest,
    branchPlanHash,
    branchPlanName,
    branches: normalizedBranches,
    inputCid,
    inputHash,
    parentRequestId,
    rewardBudget,
    simulationObjective,
    verificationPolicy
  };
}

export function expandParentIntoChildJobs(parentRequest) {
  if (!parentRequest?.branches?.length) {
    throw new Error("parentRequest.branches is required");
  }

  const childJobs = [];
  for (const branch of parentRequest.branches) {
    const executionChild = buildExecutionChildJob(parentRequest, branch);
    childJobs.push(executionChild);

    if (branch.verificationMode === "replicated-rerun") {
      childJobs.push(buildVerificationChildJob(parentRequest, executionChild, branch));
    }
  }

  return {
    aggregateChildJob: buildAggregateChildJob(parentRequest, childJobs),
    childJobs
  };
}

export function buildChildReceiptManifest({
  childJob,
  executorIdentity,
  inputCid,
  inputHash,
  outputCid,
  outputHash,
  ezklProof = null,
  zkvmReceipt = null
}) {
  if (!childJob?.childJobId) {
    throw new Error("childJob.childJobId is required");
  }
  if (!executorIdentity) {
    throw new Error("executorIdentity is required");
  }
  if (!inputCid || !inputHash || !outputCid || !outputHash) {
    throw new Error("input/output commitments are required");
  }

  const manifest = {
    branchKey: childJob.branchKey,
    childJobId: childJob.childJobId,
    executorIdentity,
    inputCid,
    inputHash,
    outputCid,
    outputHash,
    parentRequestId: childJob.parentRequestId,
    proofBundle: {
      ezkl: buildProofDescriptor(PROOF_SYSTEM.ezkl, ezklProof),
      zkvm: buildProofDescriptor(PROOF_SYSTEM.zkvm, zkvmReceipt)
    },
    role: childJob.kind,
    verificationMode: childJob.verificationMode
  };

  return {
    ...manifest,
    manifestHash: sha256Hex(Buffer.from(canonicalJson(manifest)))
  };
}

export function buildAggregateManifest(parentRequest, childReceiptManifests) {
  if (!Array.isArray(childReceiptManifests) || childReceiptManifests.length === 0) {
    throw new Error("childReceiptManifests must contain at least one child receipt");
  }
  const aggregate = {
    branchPlanHash: parentRequest.branchPlanHash,
    childReceipts: childReceiptManifests.map((receipt) => ({
      branchKey: receipt.branchKey,
      childJobId: receipt.childJobId,
      manifestHash: receipt.manifestHash,
      outputCid: receipt.outputCid,
      outputHash: receipt.outputHash
    })),
    inputCid: parentRequest.inputCid,
    inputHash: parentRequest.inputHash,
    parentRequestId: parentRequest.parentRequestId,
    verificationPolicy: parentRequest.verificationPolicy
  };

  return {
    ...aggregate,
    aggregateHash: sha256Hex(Buffer.from(canonicalJson(aggregate)))
  };
}

function normalizeBranchTemplate(branch, index) {
  const branchKey = branch.branchKey || `branch-${index + 1}`;
  return {
    branchKey,
    deterministicReducer: branch.deterministicReducer !== false,
    ezklModelDigest: branch.ezklModelDigest || null,
    highValue: Boolean(branch.highValue),
    requiredStakeMultiplier: branch.requiredStakeMultiplier || 1,
    scenarioLabel: branch.scenarioLabel || branchKey,
    verificationMode: branch.verificationMode || "proof-only",
    zkvmProgramDigest: branch.zkvmProgramDigest || null
  };
}

function buildExecutionChildJob(parentRequest, branch) {
  const proofRequirements = buildProofRequirements(branch);
  const childJobId = digestFor({
    branchKey: branch.branchKey,
    kind: BRANCH_KIND.execute,
    parentRequestId: parentRequest.parentRequestId,
    proofRequirements,
    scenarioLabel: branch.scenarioLabel
  });

  return {
    branchKey: branch.branchKey,
    childJobId,
    deterministicReducer: branch.deterministicReducer,
    kind: BRANCH_KIND.execute,
    parentRequestId: parentRequest.parentRequestId,
    proofRequirements,
    rewardShare: computeRewardShare(parentRequest, branch, 1),
    scenarioLabel: branch.scenarioLabel,
    verificationMode: branch.verificationMode
  };
}

function buildVerificationChildJob(parentRequest, executionChild, branch) {
  return {
    branchKey: branch.branchKey,
    childJobId: digestFor({
      branchKey: branch.branchKey,
      kind: BRANCH_KIND.verify,
      parentRequestId: parentRequest.parentRequestId,
      targetChildJobId: executionChild.childJobId
    }),
    kind: BRANCH_KIND.verify,
    parentRequestId: parentRequest.parentRequestId,
    rewardShare: computeRewardShare(parentRequest, branch, 0.5),
    scenarioLabel: `${branch.scenarioLabel}-verification`,
    targetChildJobId: executionChild.childJobId,
    verificationMode: "replicated-rerun"
  };
}

function buildAggregateChildJob(parentRequest, childJobs) {
  const aggregateProgramDigest =
    parentRequest.aggregateZkvmProgramDigest ||
    childJobs.find((job) => job.kind === BRANCH_KIND.execute)?.proofRequirements?.zkvm?.programDigest;
  return {
    branchKey: "aggregate",
    childJobId: digestFor({
      kind: BRANCH_KIND.aggregate,
      parentRequestId: parentRequest.parentRequestId,
      childJobIds: childJobs.map((job) => job.childJobId)
    }),
    inputChildren: childJobs.filter((job) => job.kind === BRANCH_KIND.execute).map((job) => job.childJobId),
    kind: BRANCH_KIND.aggregate,
    parentRequestId: parentRequest.parentRequestId,
    proofRequirements: {
      ezkl: null,
      zkvm: {
        programDigest: aggregateProgramDigest,
        required: true
      }
    },
    rewardShare: 0,
    scenarioLabel: "aggregate",
    verificationMode: "proof-only"
  };
}

function buildProofRequirements(branch) {
  return {
    ezkl: branch.ezklModelDigest
      ? {
          modelDigest: branch.ezklModelDigest,
          required: true
        }
      : null,
    zkvm: branch.deterministicReducer
      ? {
          programDigest: branch.zkvmProgramDigest || "deterministic-reducer-v1",
          required: true
        }
      : null
  };
}

function buildProofDescriptor(kind, proof) {
  if (!proof) {
    return null;
  }
  return {
    ...proof,
    system: kind
  };
}

function computeRewardShare(parentRequest, branch, verificationWeight) {
  const budget = Number(parentRequest.rewardBudget || 0);
  const multiplier = Number(branch.requiredStakeMultiplier || 1);
  return budget > 0 ? Math.max(1, Math.floor((budget * multiplier * verificationWeight) / parentRequest.branches.length)) : 0;
}

function digestFor(value) {
  return sha256Hex(Buffer.from(canonicalJson(value))).slice(0, 32);
}
