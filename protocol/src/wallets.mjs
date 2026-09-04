import fs from "fs";
import path from "path";
import { Keypair } from "@solana/web3.js";
import { sha256 } from "./common.mjs";

/** The runtime wallets the control plane needs. Each lives at `<sharedDir>/<name>.json`. */
export const RUNTIME_KEY_NAMES = Object.freeze(["admin", "customer", "verifier", "worker", "watcher"]);
export const KEY_FILE_MODE = 0o600;
export const KEY_DIR_MODE = 0o700;
/** Opt-in to deterministic localnet keys. Refused unless SOLANA_CLUSTER is localnet. */
export const INSECURE_SEEDS_FLAG = "KSWARM_INSECURE_LOCALNET_SEEDS";
export const LOCALNET_CLUSTER = "localnet";
/** Env var naming a keypair file outside the repository that signs program deploys and upgrades. */
export const UPGRADE_AUTHORITY_ENV = "PROTOCOL_UPGRADE_AUTHORITY_KEYPAIR";

const LOOSE_PERMISSION_BITS = 0o077;
const SECRET_KEY_LENGTH = 64;

export function keypairFromSeedText(seedText) {
  const seed = sha256(Buffer.from(seedText, "utf8")).subarray(0, 32);
  return Keypair.fromSeed(seed);
}

export function runtimeKeyPath(sharedDir, name) {
  return path.join(sharedDir, `${name}.json`);
}

/** Writes the secret key with mode 0600 in a 0700 directory. Tightens an existing loose file. */
export function writeKeypair(filePath, keypair) {
  fs.mkdirSync(path.dirname(filePath), { recursive: true, mode: KEY_DIR_MODE });
  fs.writeFileSync(filePath, JSON.stringify(Array.from(keypair.secretKey)), { mode: KEY_FILE_MODE });
  fs.chmodSync(filePath, KEY_FILE_MODE);
}

/** Throws unless `filePath` is a regular file that only its owner can read. */
export function assertPrivateKeyFile(filePath) {
  let stat;
  try {
    stat = fs.statSync(filePath);
  } catch (error) {
    if (error.code === "ENOENT") {
      throw new Error(`missing keypair file: ${filePath}`);
    }
    throw error;
  }
  if (!stat.isFile()) {
    throw new Error(`keypair path is not a regular file: ${filePath}`);
  }
  const loose = stat.mode & LOOSE_PERMISSION_BITS;
  if (process.platform !== "win32" && loose !== 0) {
    const mode = (stat.mode & 0o777).toString(8).padStart(4, "0");
    throw new Error(
      `keypair file ${filePath} is readable by group or others (mode ${mode}); refusing to load it. Fix with: chmod 600 ${filePath}`
    );
  }
}

/** Loads a keypair file. Fails closed on a missing file, loose permissions, or a malformed key. */
export function readKeypair(filePath) {
  assertPrivateKeyFile(filePath);
  let parsed;
  try {
    parsed = JSON.parse(fs.readFileSync(filePath, "utf8"));
  } catch (error) {
    throw new Error(`keypair file ${filePath} is not valid JSON: ${error.message}`);
  }
  const isByte = (value) => Number.isInteger(value) && value >= 0 && value <= 255;
  if (!Array.isArray(parsed) || parsed.length !== SECRET_KEY_LENGTH || !parsed.every(isByte)) {
    throw new Error(`keypair file ${filePath} is not a ${SECRET_KEY_LENGTH}-byte secret key array`);
  }
  return Keypair.fromSecretKey(Uint8Array.from(parsed));
}

/**
 * True when deterministic seed-derived keys are allowed. The flag is honoured only on
 * localnet; on any other cluster (or with SOLANA_CLUSTER unset) it is an error, never a fallback.
 */
export function insecureLocalnetSeedsEnabled(env = process.env) {
  if (env[INSECURE_SEEDS_FLAG] !== "1") {
    return false;
  }
  const cluster = env.SOLANA_CLUSTER;
  if (cluster !== LOCALNET_CLUSTER) {
    throw new Error(
      `${INSECURE_SEEDS_FLAG}=1 is only allowed with SOLANA_CLUSTER=${LOCALNET_CLUSTER} (SOLANA_CLUSTER is ${cluster === undefined ? "unset" : JSON.stringify(cluster)})`
    );
  }
  return true;
}

/**
 * Makes sure every runtime wallet exists. New wallets are random, generated once per
 * deployment, and written with mode 0600. Existing files are reused, never overwritten,
 * unless `options.overwrite` is set. Returns public data only.
 */
export function ensureRuntimeKeypairs(sharedDir, options = {}) {
  const env = options.env || process.env;
  const overwrite = options.overwrite || false;
  const useSeeds = insecureLocalnetSeedsEnabled(env);
  fs.mkdirSync(sharedDir, { recursive: true, mode: KEY_DIR_MODE });
  const out = {};
  for (const name of RUNTIME_KEY_NAMES) {
    const filePath = runtimeKeyPath(sharedDir, name);
    const reuse = !overwrite && fs.existsSync(filePath);
    let keypair;
    let source;
    if (reuse) {
      keypair = readKeypair(filePath);
      source = "existing";
    } else {
      keypair = useSeeds ? keypairFromSeedText(`kswarm-localnet-${name}`) : Keypair.generate();
      source = useSeeds ? "insecure-localnet-seed" : "random";
      writeKeypair(filePath, keypair);
    }
    out[name] = { filePath, publicKey: keypair.publicKey.toBase58(), source };
  }
  return out;
}

/** Loads the named runtime wallets and fails closed when any of them is missing. */
export function requireRuntimeKeypairs(sharedDir, names = RUNTIME_KEY_NAMES) {
  const missing = names.map((name) => runtimeKeyPath(sharedDir, name)).filter((file) => !fs.existsSync(file));
  if (missing.length > 0) {
    throw new Error(
      `missing required runtime keypairs: ${missing.join(", ")}; run: node protocol/scripts/write-runtime-keypairs.mjs`
    );
  }
  const out = {};
  for (const name of names) {
    out[name] = readKeypair(runtimeKeyPath(sharedDir, name));
  }
  return out;
}

/** The program upgrade authority, read from a path outside the repository. Never derived. */
export function loadUpgradeAuthorityKeypair(env = process.env) {
  const filePath = env[UPGRADE_AUTHORITY_ENV];
  if (!filePath) {
    throw new Error(`${UPGRADE_AUTHORITY_ENV} is not set; point it at a keypair file kept outside the repository`);
  }
  return readKeypair(filePath);
}

/** Creates a fresh random keypair at `filePath`. Refuses to replace an existing file unless `overwrite` is set. */
export function createRandomKeypair(filePath, options = {}) {
  if (fs.existsSync(filePath) && !options.overwrite) {
    throw new Error(`keypair file already exists: ${filePath} (pass --force to replace it)`);
  }
  const keypair = Keypair.generate();
  writeKeypair(filePath, keypair);
  return { filePath, publicKey: keypair.publicKey.toBase58() };
}
