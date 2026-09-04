import { SHARED_DIR } from "../src/runtime.mjs";
import { LOCALNET_CLUSTER, requireRuntimeKeypairs } from "../src/wallets.mjs";
import { sleep } from "../src/common.mjs";
import { Connection, sendAndConfirmTransaction, SystemProgram, Transaction } from "@solana/web3.js";

const RPC_URL = process.env.PROTOCOL_RPC_URL || "http://solana-validator:8899";
const CLUSTER = process.env.SOLANA_CLUSTER || "";
const LAMPORTS_PER_SOL = 1_000_000_000;
const RECIPIENT_TARGET = 20 * LAMPORTS_PER_SOL;
// Four recipients at 20 SOL plus fees and the admin's own transactions.
const ADMIN_TARGET = 100 * LAMPORTS_PER_SOL;

async function waitForBalance(connection, publicKey, minimumLamports, label) {
  for (let attempt = 0; attempt < 60; attempt += 1) {
    const balance = await connection.getBalance(publicKey, "confirmed");
    if (balance >= minimumLamports) {
      return balance;
    }
    await sleep(500);
  }
  throw new Error(`${label} balance did not reach ${minimumLamports} lamports`);
}

/**
 * The admin is a random per-deployment key, so it starts empty. On localnet the validator's
 * faucet funds it. Anywhere else the operator must fund it before the deployer runs.
 */
async function ensureAdminFunded(connection, admin) {
  const balance = await connection.getBalance(admin.publicKey, "confirmed");
  if (balance >= ADMIN_TARGET) {
    return;
  }
  if (CLUSTER === LOCALNET_CLUSTER) {
    await connection.requestAirdrop(admin.publicKey, ADMIN_TARGET - balance);
    await waitForBalance(connection, admin.publicKey, ADMIN_TARGET, "admin");
    return;
  }
  try {
    await waitForBalance(connection, admin.publicKey, ADMIN_TARGET, "admin");
  } catch (error) {
    throw new Error(
      `${error.message}; SOLANA_CLUSTER is ${JSON.stringify(CLUSTER)} so no airdrop is attempted. Fund ${admin.publicKey.toBase58()} with at least ${ADMIN_TARGET / LAMPORTS_PER_SOL} SOL, or set PROTOCOL_AUTO_FUND_SOL=0 and fund every runtime wallet yourself.`
    );
  }
}

async function fundAccount(connection, admin, recipient, targetLamports, label) {
  const currentBalance = await connection.getBalance(recipient.publicKey, "confirmed");
  if (currentBalance >= targetLamports) {
    return;
  }
  const lamportsNeeded = targetLamports - currentBalance;
  const tx = new Transaction().add(
    SystemProgram.transfer({
      fromPubkey: admin.publicKey,
      toPubkey: recipient.publicKey,
      lamports: lamportsNeeded
    })
  );
  await sendAndConfirmTransaction(connection, tx, [admin], {
    commitment: "confirmed"
  });
  await waitForBalance(connection, recipient.publicKey, targetLamports, label);
}

async function main() {
  const connection = new Connection(RPC_URL, "confirmed");
  const { admin, customer, verifier, worker, watcher } = requireRuntimeKeypairs(SHARED_DIR);
  await ensureAdminFunded(connection, admin);
  await Promise.all([
    fundAccount(connection, admin, customer, RECIPIENT_TARGET, "customer"),
    fundAccount(connection, admin, verifier, RECIPIENT_TARGET, "verifier"),
    fundAccount(connection, admin, worker, RECIPIENT_TARGET, "worker"),
    fundAccount(connection, admin, watcher, RECIPIENT_TARGET, "watcher")
  ]);
  console.log("funded runtime wallets");
}

main().catch((error) => {
  console.error(error);
  process.exit(1);
});
