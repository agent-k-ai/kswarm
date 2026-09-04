// The Python CLI writes protocol.json (`kswarm protocol runtime-config`);
// the Node artifact gateway and watcher read it through src/runtime.mjs. The
// fixture is produced by cli/tests/test_runtime_config.py from fixed inputs, so
// this test pins the Node side of that contract.
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { test } from "node:test";
import { TOKEN_PROGRAM_ID } from "@solana/spl-token";
import {
  humanToBaseUnits,
  runtimePaymentMint,
  runtimeStakeFloor,
  runtimeTokenProgramId,
  runtimeUnitScale
} from "../src/runtime.mjs";
import { PROGRAM_ID } from "../src/protocol.mjs";

const here = path.dirname(fileURLToPath(import.meta.url));
const fixture = JSON.parse(fs.readFileSync(path.join(here, "fixtures", "protocol.json"), "utf8"));

test("protocol.json written by the Python CLI satisfies every Node reader", () => {
  assert.equal(runtimePaymentMint(fixture).toBase58(), "CZHcDHQZerSch8Fhhi2KgV4cLiD2KtdwjJBrb8fypump");
  assert.ok(runtimeTokenProgramId(fixture).equals(TOKEN_PROGRAM_ID));
  assert.equal(runtimeUnitScale(fixture), 1000000n);
  assert.equal(runtimeStakeFloor(fixture, "tierOne"), 50000000000n);
  assert.equal(runtimeStakeFloor(fixture, "tierTwo"), 250000000000n);
  assert.equal(runtimeStakeFloor(fixture, "tierThree"), 1000000000000n);
  assert.equal(runtimeStakeFloor(fixture, "verifier"), 100000000000n);
  assert.equal(fixture.programId, PROGRAM_ID.toBase58());
  assert.equal(fixture.rpcUrl, "http://solana-validator:8899");
  assert.equal(fixture.artifactGatewayUrl, "http://protocol-api:7001");
});

test("the floors round-trip through humanToBaseUnits at the file's decimals", () => {
  const decimals = fixture.paymentDecimals;
  assert.equal(humanToBaseUnits("50000", decimals), runtimeStakeFloor(fixture, "tierOne"));
  assert.equal(humanToBaseUnits("100000", decimals), runtimeStakeFloor(fixture, "verifier"));
});

test("missing fields fail closed", () => {
  assert.throws(() => runtimeStakeFloor({}, "tierOne"), /stakeFloors\.tierOne/);
  assert.throws(() => runtimePaymentMint({}), /paymentMint/);
  assert.throws(() => runtimeUnitScale({}), /paymentDecimals/);
});
