import fs from "fs";
import path from "path";
import { Connection, PublicKey } from "@solana/web3.js";
import { sleep } from "./common.mjs";
import { requireTokenProgramId } from "./protocol.mjs";
import { readKeypair } from "./wallets.mjs";

export const SHARED_DIR = process.env.PROTOCOL_SHARED_DIR || "/runtime/protocol";
export const RUNTIME_CONFIG_PATH = path.join(SHARED_DIR, "protocol.json");
export const RUNTIME_READY_PATH = path.join(SHARED_DIR, "ready");

export async function waitForRuntimeConfig(timeoutMs = 120000) {
  const started = Date.now();
  while (Date.now() - started < timeoutMs) {
    if (fs.existsSync(RUNTIME_READY_PATH) && fs.existsSync(RUNTIME_CONFIG_PATH)) {
      return JSON.parse(fs.readFileSync(RUNTIME_CONFIG_PATH, "utf8"));
    }
    await sleep(1000);
  }
  throw new Error(`runtime config not found at ${RUNTIME_CONFIG_PATH}`);
}

export function readRuntimeConfig() {
  return JSON.parse(fs.readFileSync(RUNTIME_CONFIG_PATH, "utf8"));
}

export function runtimeConnection(runtimeConfig) {
  return new Connection(runtimeConfig.rpcUrl, "confirmed");
}

export function loadRuntimeKeypair(name) {
  return readKeypair(path.join(SHARED_DIR, `${name}.json`));
}

export function runtimePaymentMint(runtimeConfig) {
  if (!runtimeConfig.paymentMint) {
    throw new Error("protocol.json is missing paymentMint");
  }
  return new PublicKey(runtimeConfig.paymentMint);
}

export function runtimeTokenProgramId(runtimeConfig) {
  return requireTokenProgramId(runtimeConfig.tokenProgramId);
}

/** 10^decimals as a BigInt: one whole payment token in base units. */
export function runtimeUnitScale(runtimeConfig) {
  const decimals = runtimeConfig.paymentDecimals;
  if (!Number.isInteger(decimals) || decimals < 0 || decimals > 18) {
    throw new Error("protocol.json is missing a valid paymentDecimals");
  }
  return 10n ** BigInt(decimals);
}

/** A stake floor from protocol.json (`tierOne`, `tierTwo`, `tierThree`, `verifier`) in base units. */
export function runtimeStakeFloor(runtimeConfig, name) {
  const value = runtimeConfig.stakeFloors?.[name];
  if (value === undefined || value === null) {
    throw new Error(`protocol.json is missing stakeFloors.${name}`);
  }
  return BigInt(value);
}

/** Convert a human amount string (`"50000"`, `"2.5"`) to base units without floating point. */
export function humanToBaseUnits(value, decimals) {
  const trimmed = String(value).trim();
  if (!/^\d+(\.\d+)?$/.test(trimmed)) {
    throw new Error(`invalid amount: ${value}`);
  }
  const [wholePart, fractionPart = ""] = trimmed.split(".");
  if (fractionPart.length > decimals) {
    throw new Error(`amount has more than ${decimals} decimal places`);
  }
  const whole = BigInt(wholePart);
  const fraction = BigInt((fractionPart + "0".repeat(decimals)).slice(0, decimals));
  return whole * 10n ** BigInt(decimals) + fraction;
}
