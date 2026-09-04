const DEFAULT_IPFS_API_URL = process.env.PROTOCOL_IPFS_API_URL || "http://127.0.0.1:5001";

export async function addBytesToIpfs(filename, payload, ipfsApiUrl = DEFAULT_IPFS_API_URL) {
  const form = new FormData();
  form.append("file", new Blob([payload]), filename);
  const response = await fetch(`${ipfsApiUrl}/api/v0/add?pin=true&cid-version=1`, {
    method: "POST",
    body: form
  });
  if (!response.ok) {
    throw new Error(`ipfs add failed: ${response.status} ${await response.text()}`);
  }
  const lines = (await response.text())
    .split("\n")
    .map((line) => line.trim())
    .filter(Boolean);
  const parsed = JSON.parse(lines.at(-1));
  return { cid: parsed.Hash, name: parsed.Name, size: parsed.Size };
}

export async function catBytesFromIpfs(cid, ipfsApiUrl = DEFAULT_IPFS_API_URL) {
  const response = await fetch(`${ipfsApiUrl}/api/v0/cat?arg=${encodeURIComponent(cid)}`, {
    method: "POST"
  });
  if (!response.ok) {
    throw new Error(`ipfs cat failed: ${response.status} ${await response.text()}`);
  }
  return Buffer.from(await response.arrayBuffer());
}

