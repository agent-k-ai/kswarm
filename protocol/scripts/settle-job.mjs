import { getOrCreateAssociatedTokenAccount } from "@solana/spl-token";
import { PublicKey } from "@solana/web3.js";
import { sendInstructions } from "../src/client.mjs";
import {
  configPda,
  fetchJob,
  PROGRAM_ID,
  settleJobIx,
  tokenAta,
  workerPda
} from "../src/protocol.mjs";
import { loadRuntimeKeypair, readRuntimeConfig, runtimeConnection, runtimePaymentMint, runtimeTokenProgramId } from "../src/runtime.mjs";

const [, , jobArg] = process.argv;

if (!jobArg) {
  console.error("usage: node scripts/settle-job.mjs <job-address>");
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
  const workerAuthority = jobState.job.worker;
  const [worker] = await workerPda(workerAuthority, PROGRAM_ID);
  const workerPaymentAccount = await getOrCreateAssociatedTokenAccount(
    connection,
    watcher,
    paymentMint,
    tokenProgramId,
    workerAuthority,
    false,
    "confirmed",
    undefined,
    tokenProgramId
  );
  const jobEscrowVault = tokenAta(paymentMint, jobAddress, true, tokenProgramId);

  const signature = await sendInstructions(connection, watcher, [
    settleJobIx({
      caller: watcher.publicKey,
      config,
      paymentMint,
      tokenProgramId,
      job: jobAddress,
      worker,
      workerAuthority,
      jobEscrowVault,
      workerPaymentAccount: workerPaymentAccount.address
    })
  ]);

  console.log(
    JSON.stringify(
      {
        action: "settle_job",
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
