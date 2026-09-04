import { SHARED_DIR } from "../src/runtime.mjs";
import { createRandomKeypair, runtimeKeyPath } from "../src/wallets.mjs";

const args = process.argv.slice(2);
const force = args.includes("--force");
const [name] = args.filter((arg) => arg !== "--force");

if (!name || name.includes("/") || name === "." || name === "..") {
  console.error("usage: node protocol/scripts/create-runtime-keypair.mjs <name> [--force]");
  process.exit(1);
}

try {
  const created = createRandomKeypair(runtimeKeyPath(SHARED_DIR, name), { overwrite: force });
  console.log(JSON.stringify({ name, ...created }, null, 2));
} catch (error) {
  console.error(error.message);
  process.exit(1);
}
