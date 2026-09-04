import fs from "fs";
import path from "path";
import {
  createMint,
  getOrCreateAssociatedTokenAccount,
  mintTo,
  TOKEN_PROGRAM_ID
} from "@solana/spl-token";
import { SHARED_DIR, humanToBaseUnits, runtimeConnection } from "../src/runtime.mjs";
import { readKeypair } from "../src/wallets.mjs";

const RPC_URL = process.env.PROTOCOL_RPC_URL || "http://solana-validator:8899";
// The local stand-in copies the KAI layout: classic SPL Token, 6 decimals.
const DECIMALS = 6;
// Human-unit balances sized for the default floors (tier two 250,000; verifier 100,000).
const FUNDING = {
  customer: "5000000",
  verifier: "500000",
  worker: "5000000",
  watcher: "100"
};

async function fundWallet(connection, admin, paymentMint, owner, amount) {
  const account = await getOrCreateAssociatedTokenAccount(
    connection,
    admin,
    paymentMint,
    owner,
    false,
    "confirmed",
    undefined,
    TOKEN_PROGRAM_ID
  );
  await mintTo(
    connection,
    admin,
    paymentMint,
    account.address,
    admin,
    humanToBaseUnits(amount, DECIMALS),
    [],
    undefined,
    TOKEN_PROGRAM_ID
  );
  return account.address;
}

async function main() {
  const connection = runtimeConnection({ rpcUrl: RPC_URL });
  const admin = readKeypair(path.join(SHARED_DIR, "admin.json"));
  const wallets = {
    customer: readKeypair(path.join(SHARED_DIR, "customer.json")),
    verifier: readKeypair(path.join(SHARED_DIR, "verifier.json")),
    worker: readKeypair(path.join(SHARED_DIR, "worker.json")),
    watcher: readKeypair(path.join(SHARED_DIR, "watcher.json"))
  };

  const paymentMint = await createMint(
    connection,
    admin,
    admin.publicKey,
    null,
    DECIMALS,
    undefined,
    undefined,
    TOKEN_PROGRAM_ID
  );

  const tokenAccounts = {};
  for (const [name, keypair] of Object.entries(wallets)) {
    const address = await fundWallet(connection, admin, paymentMint, keypair.publicKey, FUNDING[name]);
    tokenAccounts[name] = address.toBase58();
  }

  const mintConfig = {
    decimals: DECIMALS,
    mint: paymentMint.toBase58(),
    tokenProgramId: TOKEN_PROGRAM_ID.toBase58(),
    tokenAccounts
  };
  fs.writeFileSync(path.join(SHARED_DIR, "payment-mint.json"), JSON.stringify(mintConfig, null, 2));
  console.log(JSON.stringify(mintConfig, null, 2));
}

main().catch((error) => {
  console.error(error);
  process.exit(1);
});
