import crypto from "crypto";

export function sha256(buffer) {
  return crypto.createHash("sha256").update(buffer).digest();
}

export function sha256Hex(buffer) {
  return sha256(buffer).toString("hex");
}

export function anchorInstructionDiscriminator(name) {
  return sha256(Buffer.from(`global:${name}`)).subarray(0, 8);
}

export function anchorAccountDiscriminator(name) {
  return sha256(Buffer.from(`account:${name}`)).subarray(0, 8);
}

export function u64LE(value) {
  const out = Buffer.alloc(8);
  out.writeBigUInt64LE(BigInt(value));
  return out;
}

export function i64LE(value) {
  const out = Buffer.alloc(8);
  out.writeBigInt64LE(BigInt(value));
  return out;
}

export function u32LE(value) {
  const out = Buffer.alloc(4);
  out.writeUInt32LE(Number(value));
  return out;
}

export function u16LE(value) {
  const out = Buffer.alloc(2);
  out.writeUInt16LE(Number(value));
  return out;
}

export function encodeString(value) {
  const bytes = Buffer.from(value, "utf8");
  return Buffer.concat([u32LE(bytes.length), bytes]);
}

export function encodeVec(value) {
  const bytes = Buffer.from(value);
  return Buffer.concat([u32LE(bytes.length), bytes]);
}

export function readU8(buffer, offset) {
  return [buffer.readUInt8(offset), offset + 1];
}

export function readU32(buffer, offset) {
  return [buffer.readUInt32LE(offset), offset + 4];
}

export function readU16(buffer, offset) {
  return [buffer.readUInt16LE(offset), offset + 2];
}

export function readU64(buffer, offset) {
  return [buffer.readBigUInt64LE(offset), offset + 8];
}

export function readI64(buffer, offset) {
  return [buffer.readBigInt64LE(offset), offset + 8];
}

export function readBytes(buffer, offset, length) {
  return [buffer.subarray(offset, offset + length), offset + length];
}

export function readString(buffer, offset) {
  const [length, nextOffset] = readU32(buffer, offset);
  return [buffer.subarray(nextOffset, nextOffset + length).toString("utf8"), nextOffset + length];
}

export function readVec(buffer, offset) {
  const [length, nextOffset] = readU32(buffer, offset);
  return [buffer.subarray(nextOffset, nextOffset + length), nextOffset + length];
}

export function canonicalJson(value) {
  return JSON.stringify(sortKeys(value));
}

function sortKeys(value) {
  if (Array.isArray(value)) {
    return value.map(sortKeys);
  }
  if (value && typeof value === "object" && !(value instanceof Uint8Array) && !Buffer.isBuffer(value)) {
    return Object.keys(value)
      .sort()
      .reduce((accumulator, key) => {
        accumulator[key] = sortKeys(value[key]);
        return accumulator;
      }, {});
  }
  return value;
}

export function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}
