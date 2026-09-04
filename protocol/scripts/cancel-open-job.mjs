import { getOrCreateAssociatedTokenAccount } from "@solana/spl-token";
import { PublicKey } from "@solana/web3.js";
import { sendInstructions } from "../src/client.mjs";
import {
  cancelOpenJobIx,
  configPda,
  fetchJob,
  PROGRAM_ID,
  tokenAta
} from "../src/protocol.mjs";
import { loadRuntimeKeypair, readRuntimeConfig, runtimeConnection, runtimePaymentMint, runtimeTokenProgramId } from "../src/runtime.mjs";

const [, , jobArg] = process.argv;

if (!jobArg) {
  console.error("usage: node scripts/cancel-open-job.mjs <job-address>");
  process.exit(1);
}

async function main() {
  const runtimeConfig = readRuntimeConfig();
  const connection = runtimeConnection(runtimeConfig);
  const customer = loadRuntimeKeypair("customer");
  const paymentMint = runtimePaymentMint(runtimeConfig);
  const tokenProgramId = runtimeTokenProgramId(runtimeConfig);
  const [config] = await configPda(PROGRAM_ID);
  const jobAddress = new PublicKey(jobArg);
  const jobState = await fetchJob(connection, jobAddress);
  if (!jobState) {
    throw new Error(`job not found: ${jobArg}`);
  }
  if (!jobState.job.customer.equals(customer.publicKey)) {
    throw new Error("runtime customer is not the owner of this job");
  }
  const customerPaymentAccount = await getOrCreateAssociatedTokenAccount(
    connection,
    customer,
    paymentMint,
    tokenProgramId,
    customer.publicKey,
    false,
    "confirmed",
    undefined,
    tokenProgramId
  );
  const jobEscrowVault = tokenAta(paymentMint, jobAddress, true, tokenProgramId);

  const signature = await sendInstructions(connection, customer, [
    cancelOpenJobIx({
      customer: customer.publicKey,
      config,
      paymentMint,
      tokenProgramId,
      job: jobAddress,
      jobEscrowVault,
      customerPaymentAccount: customerPaymentAccount.address
    })
  ]);

  console.log(
    JSON.stringify(
      {
        action: "cancel_open_job",
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
