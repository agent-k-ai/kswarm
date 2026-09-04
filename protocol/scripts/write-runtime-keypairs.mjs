import { SHARED_DIR } from "../src/runtime.mjs";
import { ensureRuntimeKeypairs } from "../src/wallets.mjs";

// Random keys, generated once per deployment. Seeded keys need
// KSWARM_INSECURE_LOCALNET_SEEDS=1 and SOLANA_CLUSTER=localnet. Prints public data only.
const written = ensureRuntimeKeypairs(SHARED_DIR);
console.log(JSON.stringify(written, null, 2));
