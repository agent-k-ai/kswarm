import express from "express";
import multer from "multer";
import nacl from "tweetnacl";
import { PublicKey } from "@solana/web3.js";
import { sha256Hex, canonicalJson } from "./common.mjs";
import { addBytesToIpfs, catBytesFromIpfs } from "./ipfs.mjs";
import { fetchJob } from "./protocol.mjs";
import { waitForRuntimeConfig, runtimeConnection } from "./runtime.mjs";

const upload = multer();

function verifySignedPayload(auth) {
  const message = Buffer.from(
    canonicalJson({
      job: auth.job,
      kind: auth.kind,
      sha256: auth.sha256,
      timestamp: auth.timestamp
    }),
    "utf8"
  );
  const publicKey = new PublicKey(auth.signer);
  const signature = Buffer.from(auth.signature, "base64");
  const valid = nacl.sign.detached.verify(message, signature, publicKey.toBytes());
  if (!valid) {
    throw new Error("invalid artifact authorization signature");
  }
}

function requireFreshTimestamp(timestamp) {
  const delta = Math.abs(Date.now() - Number(timestamp));
  if (delta > 5 * 60 * 1000) {
    throw new Error("stale artifact authorization timestamp");
  }
}

async function start() {
  const runtimeConfig = await waitForRuntimeConfig();
  const connection = runtimeConnection(runtimeConfig);
  const app = express();
  const ipfsApiUrl = process.env.PROTOCOL_IPFS_API_URL || "http://ipfs-bootstrap:5001";
  app.get("/health", (_req, res) => {
    res.json({ ok: true });
  });

  app.post("/api/protocol/jobs/:jobAddress/input", upload.single("artifact"), async (req, res, next) => {
    try {
      const file = req.file;
      if (!file) {
        throw new Error("missing artifact file");
      }
      const auth = JSON.parse(req.body.auth || "{}");
      verifySignedPayload(auth);
      requireFreshTimestamp(auth.timestamp);
      const jobState = await fetchJob(connection, req.params.jobAddress);
      if (!jobState) {
        throw new Error("job not found");
      }
      const job = jobState.job;
      if (job.customer.toBase58() !== auth.signer) {
        throw new Error("only the customer can upload input");
      }
      if (job.status !== 1) {
        throw new Error("job is not awaiting artifact upload");
      }
      const observedHash = sha256Hex(file.buffer);
      if (observedHash !== Buffer.from(job.inputBundleHash).toString("hex")) {
        throw new Error("input bundle hash mismatch");
      }
      if (auth.sha256 !== observedHash || auth.kind !== "input" || auth.job !== req.params.jobAddress) {
        throw new Error("artifact authorization payload mismatch");
      }
      const artifact = await addBytesToIpfs(file.originalname || "input.bin", file.buffer, ipfsApiUrl);
      res.json({ success: true, data: artifact });
    } catch (error) {
      next(error);
    }
  });

  app.post("/api/protocol/jobs/:jobAddress/output", upload.single("artifact"), async (req, res, next) => {
    try {
      const file = req.file;
      if (!file) {
        throw new Error("missing artifact file");
      }
      const auth = JSON.parse(req.body.auth || "{}");
      verifySignedPayload(auth);
      requireFreshTimestamp(auth.timestamp);
      const jobState = await fetchJob(connection, req.params.jobAddress);
      if (!jobState) {
        throw new Error("job not found");
      }
      const job = jobState.job;
      if (job.worker.toBase58() !== auth.signer) {
        throw new Error("only the assigned worker can upload output");
      }
      if (![3, 4].includes(job.status)) {
        throw new Error("job is not in a state that allows output uploads");
      }
      const observedHash = sha256Hex(file.buffer);
      if (auth.sha256 !== observedHash || auth.kind !== "output" || auth.job !== req.params.jobAddress) {
        throw new Error("artifact authorization payload mismatch");
      }
      const artifact = await addBytesToIpfs(file.originalname || "output.json", file.buffer, ipfsApiUrl);
      res.json({ success: true, data: artifact });
    } catch (error) {
      next(error);
    }
  });

  app.get("/api/protocol/artifacts/:cid", async (req, res, next) => {
    try {
      const payload = await catBytesFromIpfs(req.params.cid, ipfsApiUrl);
      res.type("application/octet-stream").send(payload);
    } catch (error) {
      next(error);
    }
  });

  app.use((error, _req, res, _next) => {
    res.status(400).json({ success: false, error: error.message });
  });

  const port = Number(process.env.PROTOCOL_PORT || "7001");
  app.listen(port, "0.0.0.0");
}

start().catch((error) => {
  console.error(error);
  process.exit(1);
});

