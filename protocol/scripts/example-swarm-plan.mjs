import { buildAggregateManifest, buildChildReceiptManifest, buildParentSwarmRequest, expandParentIntoChildJobs } from "../src/swarm.mjs";

const parentRequest = buildParentSwarmRequest({
  parentRequestId: "demo-parent-request-001",
  inputCid: "bafybeigdyrzt-example-input",
  inputHash: "4d4f434b5f494e5055545f48415348",
  rewardBudget: 300,
  simulationObjective: "Explore baseline, optimistic, pessimistic, and tariff shock trajectories.",
  branchTemplates: [
    {
      branchKey: "baseline",
      scenarioLabel: "Baseline world",
      ezklModelDigest: "onnx-risk-score-v1",
      zkvmProgramDigest: "branch-reducer-v1"
    },
    {
      branchKey: "optimistic",
      scenarioLabel: "Optimistic world",
      ezklModelDigest: "onnx-risk-score-v1",
      zkvmProgramDigest: "branch-reducer-v1"
    },
    {
      branchKey: "pessimistic",
      scenarioLabel: "Pessimistic world",
      ezklModelDigest: "onnx-risk-score-v1",
      zkvmProgramDigest: "branch-reducer-v1",
      highValue: true,
      verificationMode: "replicated-rerun"
    },
    {
      branchKey: "tariff-shock",
      scenarioLabel: "Tariff shock",
      ezklModelDigest: "onnx-policy-impact-v1",
      zkvmProgramDigest: "branch-reducer-v1",
      highValue: true
    }
  ]
});

const swarmPlan = expandParentIntoChildJobs(parentRequest);
const exampleReceipts = swarmPlan.childJobs
  .filter((job) => job.kind === "execute")
  .map((job) =>
    buildChildReceiptManifest({
      childJob: job,
      executorIdentity: `worker-for-${job.branchKey}`,
      inputCid: parentRequest.inputCid,
      inputHash: parentRequest.inputHash,
      outputCid: `bafy-output-${job.branchKey}`,
      outputHash: `hash-output-${job.branchKey}`,
      ezklProof: job.proofRequirements?.ezkl
        ? {
            proofCid: `bafy-ezkl-${job.branchKey}`,
            publicOutputHash: `public-output-${job.branchKey}`
          }
        : null,
      zkvmReceipt: job.proofRequirements?.zkvm
        ? {
            journalHash: `journal-${job.branchKey}`,
            receiptCid: `bafy-zkvm-${job.branchKey}`
          }
        : null
    })
  );

const aggregateManifest = buildAggregateManifest(parentRequest, exampleReceipts);

console.log(
  JSON.stringify(
    {
      aggregateManifest,
      parentRequest,
      swarmPlan
    },
    null,
    2
  )
);
