import { PublicKey } from "@solana/web3.js";
import { sleep } from "../src/common.mjs";
import {
  claimCustomerSlashCompensationIx,
  claimVerifierSlashRewardIx,
  configPda,
  fetchAllJobs,
  JOB_STATUS,
  refundSlashedJobEscrowIx,
  settleJobIx,
  slashStaleJobIx,
  tokenAta,
  workerPda,
  PROGRAM_ID
} from "../src/protocol.mjs";
import { loadRuntimeKeypair, runtimeConnection, runtimePaymentMint, runtimeTokenProgramId, waitForRuntimeConfig } from "../src/runtime.mjs";
import { sendInstructions } from "../src/client.mjs";

const SETTLEMENT_SAFETY_SECONDS = 2n;

async function main() {
  const runtimeConfig = await waitForRuntimeConfig();
  const connection = runtimeConnection(runtimeConfig);
  const watcher = loadRuntimeKeypair("watcher");
  const [config] = await configPda(PROGRAM_ID);
  const paymentMint = runtimePaymentMint(runtimeConfig);
  const tokenProgramId = runtimeTokenProgramId(runtimeConfig);
  while (true) {
    const now = BigInt(Math.floor(Date.now() / 1000));
    const jobs = await fetchAllJobs(connection);
    for (const { publicKey, job } of jobs) {
      try {
        if (
          job.status === JOB_STATUS.completed &&
          job.challengeDeadline + SETTLEMENT_SAFETY_SECONDS <= now
        ) {
          const [worker] = await workerPda(job.worker, PROGRAM_ID);
          const workerPaymentAccount = tokenAta(paymentMint, job.worker, false, tokenProgramId);
          const jobEscrowVault = tokenAta(paymentMint, publicKey, true, tokenProgramId);
          await sendInstructions(connection, watcher, [
            settleJobIx({
              caller: watcher.publicKey,
              config,
              paymentMint,
              tokenProgramId,
              job: publicKey,
              worker,
              workerAuthority: job.worker,
              jobEscrowVault,
              workerPaymentAccount
            })
          ]);
        } else if (job.status === JOB_STATUS.slashed && !job.slashSettled) {
          const [worker] = await workerPda(job.worker, PROGRAM_ID);
          const customerPaymentAccount = tokenAta(paymentMint, job.customer, false, tokenProgramId);
          const verifierRewardAccount = tokenAta(paymentMint, job.challenger, false, tokenProgramId);
          const workerStakeVault = tokenAta(paymentMint, worker, true, tokenProgramId);
          const jobEscrowVault = tokenAta(paymentMint, publicKey, true, tokenProgramId);
          const slashInstructions = [
            refundSlashedJobEscrowIx({
              caller: watcher.publicKey,
              config,
              paymentMint,
              tokenProgramId,
              job: publicKey,
              customerAuthority: job.customer,
              customerPaymentAccount,
              jobEscrowVault
            }),
            claimVerifierSlashRewardIx({
              caller: watcher.publicKey,
              config,
              paymentMint,
              tokenProgramId,
              job: publicKey,
              verifierAuthority: job.challenger,
              verifierRewardAccount,
              worker,
              workerAuthority: job.worker,
              workerStakeVault
            })
          ];
          if (job.requiredStake > job.challengeBond) {
            slashInstructions.push(
              claimCustomerSlashCompensationIx({
                caller: watcher.publicKey,
                config,
                paymentMint,
                tokenProgramId,
                job: publicKey,
                customerAuthority: job.customer,
                customerPaymentAccount,
                worker,
                workerAuthority: job.worker,
                workerStakeVault
              })
            );
          }
          await sendInstructions(connection, watcher, slashInstructions);
        } else if (
          job.status === JOB_STATUS.claimed &&
          job.executeDeadline + SETTLEMENT_SAFETY_SECONDS < now
        ) {
          const [worker] = await workerPda(job.worker, PROGRAM_ID);
          const customerPaymentAccount = tokenAta(paymentMint, job.customer, false, tokenProgramId);
          const workerStakeVault = tokenAta(paymentMint, worker, true, tokenProgramId);
          const jobEscrowVault = tokenAta(paymentMint, publicKey, true, tokenProgramId);
          await sendInstructions(connection, watcher, [
            slashStaleJobIx({
              caller: watcher.publicKey,
              config,
              paymentMint,
              tokenProgramId,
              job: publicKey,
              customerAuthority: job.customer,
              customerPaymentAccount,
              worker,
              workerAuthority: job.worker,
              workerStakeVault,
              jobEscrowVault
            })
          ]);
        }
      } catch (error) {
        if (
          error.message.includes("ChallengeWindowOpen") ||
          error.message.includes("ExecutionWindowOpen")
        ) {
          continue;
        }
        console.error(`[watcher] ${publicKey.toBase58()} ${error.message}`);
      }
    }
    await sleep(2000);
  }
}

main().catch((error) => {
  console.error(error);
  process.exit(1);
});
