import { getMint, getOrCreateAssociatedTokenAccount } from "@solana/spl-token";
import { PublicKey } from "@solana/web3.js";
import { sendInstructions } from "../src/client.mjs";
import {
  configPda,
  fetchWorker,
  PROGRAM_ID,
  tokenAta,
  withdrawUnlockedStakeIx,
  workerPda
} from "../src/protocol.mjs";
import { humanToBaseUnits, loadRuntimeKeypair, readRuntimeConfig, runtimeConnection, runtimePaymentMint, runtimeTokenProgramId } from "../src/runtime.mjs";

const [, , amountArg] = process.argv;

if (!amountArg) {
  console.error("usage: node scripts/withdraw-unlocked-stake.mjs <amount>");
  process.exit(1);
}

async function main() {
  const runtimeConfig = readRuntimeConfig();
  const connection = runtimeConnection(runtimeConfig);
  const workerAuthority = loadRuntimeKeypair("worker");
  const paymentMint = runtimePaymentMint(runtimeConfig);
  const tokenProgramId = runtimeTokenProgramId(runtimeConfig);
  const mint = await getMint(connection, paymentMint, "confirmed", tokenProgramId);
  const amount = humanToBaseUnits(amountArg, mint.decimals);
  const [config] = await configPda(PROGRAM_ID);
  const [worker] = await workerPda(workerAuthority.publicKey, PROGRAM_ID);
  const workerState = await fetchWorker(connection, worker);
  if (!workerState) {
    throw new Error("worker account is not registered");
  }
  const workerStakeVault = tokenAta(paymentMint, worker, true, tokenProgramId);
  const workerDestinationAccount = await getOrCreateAssociatedTokenAccount(
    connection,
    workerAuthority,
    paymentMint,
    tokenProgramId,
    workerAuthority.publicKey,
    false,
    "confirmed",
    undefined,
    tokenProgramId
  );

  const signature = await sendInstructions(connection, workerAuthority, [
    withdrawUnlockedStakeIx({
      authority: workerAuthority.publicKey,
      config,
      paymentMint,
      tokenProgramId,
      worker,
      workerStakeVault,
      workerDestinationAccount: workerDestinationAccount.address,
      amount
    })
  ]);

  console.log(
    JSON.stringify(
      {
        action: "withdraw_unlocked_stake",
        amount: amount.toString(),
        amountUi: amountArg,
        worker: worker.toBase58(),
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
