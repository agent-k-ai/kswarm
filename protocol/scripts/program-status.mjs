import { Connection } from "@solana/web3.js";
import { PROGRAM_ID } from "../src/protocol.mjs";

// Exit 0 when an executable account exists at the declared program id, 3 when it does not,
// 1 on any RPC error. `--print-id` prints the declared id and exits 0 without touching RPC.
const RPC_URL = process.env.PROTOCOL_RPC_URL || "http://solana-validator:8899";

if (process.argv.includes("--print-id")) {
  console.log(PROGRAM_ID.toBase58());
  process.exit(0);
}

try {
  const connection = new Connection(RPC_URL, "confirmed");
  const info = await connection.getAccountInfo(PROGRAM_ID, "confirmed");
  const deployed = Boolean(info && info.executable);
  console.log(JSON.stringify({ programId: PROGRAM_ID.toBase58(), rpcUrl: RPC_URL, deployed }));
  process.exit(deployed ? 0 : 3);
} catch (error) {
  console.error(`program-status: ${error.message}`);
  process.exit(1);
}
