import nacl from "tweetnacl";
import { Transaction, sendAndConfirmTransaction } from "@solana/web3.js";
import { canonicalJson, sha256Hex } from "./common.mjs";

export async function sendInstructions(connection, payer, instructions, signers = []) {
  const transaction = new Transaction().add(...instructions);
  return sendAndConfirmTransaction(connection, transaction, [payer, ...signers], {
    commitment: "confirmed"
  });
}

export function signArtifactAuthorization(keypair, payload) {
  const message = Buffer.from(
    canonicalJson({
      job: payload.job,
      kind: payload.kind,
      sha256: payload.sha256,
      timestamp: payload.timestamp
    }),
    "utf8"
  );
  const signature = nacl.sign.detached(message, keypair.secretKey);
  return {
    ...payload,
    signature: Buffer.from(signature).toString("base64"),
    signer: keypair.publicKey.toBase58()
  };
}

export async function uploadArtifact({
  artifactGatewayUrl,
  endpointPath,
  keypair,
  jobAddress,
  kind,
  filename,
  payload
}) {
  const auth = signArtifactAuthorization(keypair, {
    job: jobAddress,
    kind,
    sha256: sha256Hex(payload),
    timestamp: Date.now()
  });
  const form = new FormData();
  form.append("artifact", new Blob([payload]), filename);
  form.append("auth", JSON.stringify(auth));
  const response = await fetch(`${artifactGatewayUrl}${endpointPath}`, {
    method: "POST",
    body: form
  });
  const parsed = await response.json();
  if (!response.ok || !parsed.success) {
    throw new Error(parsed.error || `artifact upload failed: ${response.status}`);
  }
  return parsed.data;
}

