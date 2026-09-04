import { test } from "node:test";
import assert from "node:assert/strict";
import fs from "fs";
import os from "os";
import path from "path";
import { Keypair } from "@solana/web3.js";
import {
  INSECURE_SEEDS_FLAG,
  KEY_DIR_MODE,
  KEY_FILE_MODE,
  RUNTIME_KEY_NAMES,
  UPGRADE_AUTHORITY_ENV,
  createRandomKeypair,
  ensureRuntimeKeypairs,
  insecureLocalnetSeedsEnabled,
  loadUpgradeAuthorityKeypair,
  readKeypair,
  requireRuntimeKeypairs,
  runtimeKeyPath,
  writeKeypair
} from "../src/wallets.mjs";

// sha256("kswarm-localnet-admin")[0..32] as an ed25519 seed. Public, and therefore burned.
const SEEDED_ADMIN_PUBKEY = "BHRVHHwRztq63T8NstArk1m5iZmDa3V7zAR732JAqaxq";
const NO_SEEDS = Object.freeze({});
const LOCALNET_SEEDS = Object.freeze({ [INSECURE_SEEDS_FLAG]: "1", SOLANA_CLUSTER: "localnet" });

function tmpDir() {
  return fs.mkdtempSync(path.join(os.tmpdir(), "kswarm-wallets-"));
}

function mode(filePath) {
  return fs.statSync(filePath).mode & 0o777;
}

test("ensureRuntimeKeypairs writes random 0600 keys in a 0700 directory", () => {
  const dir = path.join(tmpDir(), "protocol");
  const first = ensureRuntimeKeypairs(dir, { env: NO_SEEDS });
  assert.deepEqual(Object.keys(first), [...RUNTIME_KEY_NAMES]);
  assert.equal(mode(dir), KEY_DIR_MODE);
  for (const name of RUNTIME_KEY_NAMES) {
    assert.equal(first[name].filePath, runtimeKeyPath(dir, name));
    assert.equal(first[name].source, "random");
    assert.equal(mode(first[name].filePath), KEY_FILE_MODE);
    assert.equal(readKeypair(first[name].filePath).publicKey.toBase58(), first[name].publicKey);
  }
  assert.notEqual(first.admin.publicKey, SEEDED_ADMIN_PUBKEY);

  const other = ensureRuntimeKeypairs(path.join(tmpDir(), "protocol"), { env: NO_SEEDS });
  assert.notEqual(other.admin.publicKey, first.admin.publicKey, "keys are random per deployment");
});

test("ensureRuntimeKeypairs reuses existing keys and never overwrites them by default", () => {
  const dir = tmpDir();
  const first = ensureRuntimeKeypairs(dir, { env: NO_SEEDS });
  const again = ensureRuntimeKeypairs(dir, { env: NO_SEEDS });
  for (const name of RUNTIME_KEY_NAMES) {
    assert.equal(again[name].publicKey, first[name].publicKey);
    assert.equal(again[name].source, "existing");
  }
  const replaced = ensureRuntimeKeypairs(dir, { env: NO_SEEDS, overwrite: true });
  assert.notEqual(replaced.admin.publicKey, first.admin.publicKey);
});

test("seed-derived keys need the explicit flag and localnet", () => {
  assert.equal(insecureLocalnetSeedsEnabled(NO_SEEDS), false);
  assert.equal(insecureLocalnetSeedsEnabled({ [INSECURE_SEEDS_FLAG]: "0", SOLANA_CLUSTER: "localnet" }), false);
  assert.equal(insecureLocalnetSeedsEnabled(LOCALNET_SEEDS), true);
  assert.throws(
    () => insecureLocalnetSeedsEnabled({ [INSECURE_SEEDS_FLAG]: "1" }),
    /only allowed with SOLANA_CLUSTER=localnet .*unset/
  );
  assert.throws(
    () => insecureLocalnetSeedsEnabled({ [INSECURE_SEEDS_FLAG]: "1", SOLANA_CLUSTER: "devnet" }),
    /only allowed with SOLANA_CLUSTER=localnet .*"devnet"/
  );
  assert.throws(
    () => insecureLocalnetSeedsEnabled({ [INSECURE_SEEDS_FLAG]: "1", SOLANA_CLUSTER: "mainnet-beta" }),
    /only allowed with SOLANA_CLUSTER=localnet/
  );
});

test("ensureRuntimeKeypairs derives the seeded keys only under the flag on localnet", () => {
  const seeded = ensureRuntimeKeypairs(tmpDir(), { env: LOCALNET_SEEDS });
  assert.equal(seeded.admin.publicKey, SEEDED_ADMIN_PUBKEY);
  assert.equal(seeded.admin.source, "insecure-localnet-seed");
  assert.equal(mode(seeded.admin.filePath), KEY_FILE_MODE);

  const dir = tmpDir();
  assert.throws(
    () => ensureRuntimeKeypairs(dir, { env: { [INSECURE_SEEDS_FLAG]: "1", SOLANA_CLUSTER: "devnet" } }),
    /only allowed with SOLANA_CLUSTER=localnet/
  );
  assert.equal(fs.readdirSync(dir).length, 0, "nothing is written when the flag is refused");
});

test("readKeypair fails closed on a missing, loose, or malformed file", () => {
  const dir = tmpDir();
  const missing = path.join(dir, "missing.json");
  assert.throws(() => readKeypair(missing), /missing keypair file/);

  const loose = path.join(dir, "loose.json");
  writeKeypair(loose, Keypair.generate());
  fs.chmodSync(loose, 0o644);
  assert.throws(() => readKeypair(loose), /readable by group or others \(mode 0644\).*chmod 600/);
  fs.chmodSync(loose, 0o640);
  assert.throws(() => readKeypair(loose), /mode 0640/);
  fs.chmodSync(loose, 0o600);
  assert.ok(readKeypair(loose));
  fs.chmodSync(loose, 0o400);
  assert.ok(readKeypair(loose), "read-only for the owner is fine");

  const short = path.join(dir, "short.json");
  fs.writeFileSync(short, JSON.stringify([1, 2, 3]), { mode: KEY_FILE_MODE });
  assert.throws(() => readKeypair(short), /not a 64-byte secret key array/);

  const junk = path.join(dir, "junk.json");
  fs.writeFileSync(junk, "not json", { mode: KEY_FILE_MODE });
  assert.throws(() => readKeypair(junk), /not valid JSON/);

  const directory = path.join(dir, "dir.json");
  fs.mkdirSync(directory, { mode: KEY_DIR_MODE });
  assert.throws(() => readKeypair(directory), /not a regular file/);
});

test("writeKeypair tightens an existing loose file", () => {
  const filePath = path.join(tmpDir(), "admin.json");
  fs.writeFileSync(filePath, "[]", { mode: 0o644 });
  assert.equal(mode(filePath), 0o644);
  writeKeypair(filePath, Keypair.generate());
  assert.equal(mode(filePath), KEY_FILE_MODE);
});

test("requireRuntimeKeypairs lists every missing wallet and loads the rest", () => {
  const dir = tmpDir();
  assert.throws(() => requireRuntimeKeypairs(dir), (error) => {
    assert.match(error.message, /missing required runtime keypairs/);
    for (const name of RUNTIME_KEY_NAMES) {
      assert.ok(error.message.includes(runtimeKeyPath(dir, name)), `mentions ${name}`);
    }
    assert.match(error.message, /write-runtime-keypairs\.mjs/);
    return true;
  });
  const written = ensureRuntimeKeypairs(dir, { env: NO_SEEDS });
  const loaded = requireRuntimeKeypairs(dir);
  assert.equal(loaded.worker.publicKey.toBase58(), written.worker.publicKey);
  fs.rmSync(written.watcher.filePath);
  assert.throws(() => requireRuntimeKeypairs(dir), new RegExp(`missing required runtime keypairs: ${written.watcher.filePath.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")};`));
  assert.ok(requireRuntimeKeypairs(dir, ["worker"]));
});

test("createRandomKeypair refuses to replace an existing file unless forced", () => {
  const filePath = path.join(tmpDir(), "worker.json");
  const created = createRandomKeypair(filePath);
  assert.equal(mode(filePath), KEY_FILE_MODE);
  assert.throws(() => createRandomKeypair(filePath), /already exists.*--force/);
  assert.equal(readKeypair(filePath).publicKey.toBase58(), created.publicKey);
  const replaced = createRandomKeypair(filePath, { overwrite: true });
  assert.notEqual(replaced.publicKey, created.publicKey);
});

test("loadUpgradeAuthorityKeypair reads only from the configured external path", () => {
  assert.throws(() => loadUpgradeAuthorityKeypair({}), new RegExp(`${UPGRADE_AUTHORITY_ENV} is not set`));
  const filePath = path.join(tmpDir(), "upgrade-authority.json");
  const keypair = Keypair.generate();
  writeKeypair(filePath, keypair);
  const loaded = loadUpgradeAuthorityKeypair({ [UPGRADE_AUTHORITY_ENV]: filePath });
  assert.equal(loaded.publicKey.toBase58(), keypair.publicKey.toBase58());
  fs.chmodSync(filePath, 0o644);
  assert.throws(() => loadUpgradeAuthorityKeypair({ [UPGRADE_AUTHORITY_ENV]: filePath }), /chmod 600/);
});
