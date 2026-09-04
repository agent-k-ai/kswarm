import { getOrCreateAssociatedTokenAccount } from "@solana/spl-token";
import { PublicKey } from "@solana/web3.js";
import { sendInstructions } from "../src/client.mjs";
import {
  configPda,
  fetchJob,
  PROGRAM_ID,
  slashStaleJobIx,
  tokenAta,
  workerPda
} from "../src/protocol.mjs";
import { loadRuntimeKeypair, readRuntimeConfig, runtimeConnection, runtimePaymentMint, runtimeTokenProgramId } from "../src/runtime.mjs";

const [, , jobArg] = process.argv;

if (!jobArg) {
  console.error("usage: node scripts/slash-stale-job.mjs <job-address>");
  process.exit(1);
}

async function main() {
  const runtimeConfig = readRuntimeConfig();
  const connection = runtimeConnection(runtimeConfig);
  const watcher = loadRuntimeKeypair("watcher");
  const paymentMint = runtimePaymentMint(runtimeConfig);
  const tokenProgramId = runtimeTokenProgramId(runtimeConfig);
  const [config] = await configPda(PROGRAM_ID);
  const jobAddress = new PublicKey(jobArg);
  const jobState = await fetchJob(connection, jobAddress);
  if (!jobState) {
    throw new Error(`job not found: ${jobArg}`);
  }
  const customerAuthority = jobState.job.customer;
  const workerAuthority = jobState.job.worker;
  const [worker] = await workerPda(workerAuthority, PROGRAM_ID);
  const customerPaymentAccount = await getOrCreateAssociatedTokenAccount(
    connection,
    watcher,
    paymentMint,
    tokenProgramId,
    customerAuthority,
    false,
    "confirmed",
    undefined,
    tokenProgramId
  );
  const workerStakeVault = tokenAta(paymentMint, worker, true, tokenProgramId);
  const jobEscrowVault = tokenAta(paymentMint, jobAddress, true, tokenProgramId);

  const signature = await sendInstructions(connection, watcher, [
    slashStaleJobIx({
      caller: watcher.publicKey,
      config,
      paymentMint,
      tokenProgramId,
      job: jobAddress,
      customerAuthority,
      customerPaymentAccount: customerPaymentAccount.address,
      worker,
      workerAuthority,
      workerStakeVault,
      jobEscrowVault
    })
  ]);

  console.log(
    JSON.stringify(
      {
        action: "slash_stale_job",
        job: jobAddress.toBase58(),
        signature
      },
      null,
      2
    )
  );
}

main().catch((error) => {
  console.error(error);
  process.exit(1);
});
