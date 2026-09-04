import {
  ASSOCIATED_TOKEN_PROGRAM_ID,
  TOKEN_2022_PROGRAM_ID,
  TOKEN_PROGRAM_ID,
  getAssociatedTokenAddressSync
} from "@solana/spl-token";
import {
  PublicKey,
  TransactionInstruction
} from "@solana/web3.js";
import {
  anchorAccountDiscriminator,
  anchorInstructionDiscriminator,
  readBytes,
  readI64,
  readString,
  readU16,
  readVec,
  readU32,
  readU64,
  readU8,
  u64LE
} from "./common.mjs";

export const PROGRAM_ID = new PublicKey("ERNzRcYhX6UYboXAAP7vwzbCKsULYu21R4RFNvDD8CkM");
export { ASSOCIATED_TOKEN_PROGRAM_ID };

/**
 * The payment mint's owner program, as pinned in `ProtocolConfig.token_program`.
 * Every token instruction must carry this exact program; nothing is hardcoded.
 */
export function requireTokenProgramId(value) {
  if (!value) {
    throw new Error("tokenProgramId is required; read it from protocol.json");
  }
  const tokenProgramId = new PublicKey(value);
  if (!tokenProgramId.equals(TOKEN_PROGRAM_ID) && !tokenProgramId.equals(TOKEN_2022_PROGRAM_ID)) {
    throw new Error(`unsupported token program ${tokenProgramId.toBase58()}`);
  }
  return tokenProgramId;
}

export const JOB_STATUS = {
  awaitingArtifact: 1,
  open: 2,
  claimed: 3,
  completed: 4,
  settled: 5,
  cancelled: 6,
  slashed: 7,
  cancelledOnExhaustion: 8,
  cancelledOnTimeout: 9
};

export async function configPda(programId = PROGRAM_ID) {
  return PublicKey.findProgramAddressSync([Buffer.from("config")], programId);
}

export async function workerPda(authority, programId = PROGRAM_ID) {
  return PublicKey.findProgramAddressSync([Buffer.from("worker"), authority.toBuffer()], programId);
}

export function tokenAta(mint, owner, allowOwnerOffCurve, tokenProgramId) {
  return getAssociatedTokenAddressSync(
    mint,
    owner,
    allowOwnerOffCurve,
    requireTokenProgramId(tokenProgramId),
    ASSOCIATED_TOKEN_PROGRAM_ID
  );
}

export function encodeWithdrawUnlockedStake(amount) {
  return Buffer.concat([anchorInstructionDiscriminator("withdraw_unlocked_stake"), u64LE(amount)]);
}

export function encodeSettleJob() {
  return anchorInstructionDiscriminator("settle_job");
}

export function encodeRefundSlashedJobEscrow() {
  return anchorInstructionDiscriminator("refund_slashed_job_escrow");
}

export function encodeClaimVerifierSlashReward() {
  return anchorInstructionDiscriminator("claim_verifier_slash_reward");
}

export function encodeClaimCustomerSlashCompensation() {
  return anchorInstructionDiscriminator("claim_customer_slash_compensation");
}

export function encodeCancelOpenJob() {
  return anchorInstructionDiscriminator("cancel_open_job");
}

export function encodeSlashStaleJob() {
  return anchorInstructionDiscriminator("slash_stale_job");
}

export function decodeWorker(data) {
  validateDiscriminator(data, "Worker");
  let offset = 8;
  let bump;
  [bump, offset] = readU8(data, offset);
  let authority;
  [authority, offset] = readBytes(data, offset, 32);
  let stakeVault;
  [stakeVault, offset] = readBytes(data, offset, 32);
  let lockedStake;
  [lockedStake, offset] = readU64(data, offset);
  let activeClaims;
  [activeClaims, offset] = readU16(data, offset);
  let registeredAt;
  [registeredAt, offset] = readI64(data, offset);
  let status;
  [status, offset] = readU8(data, offset);
  let role;
  [role, offset] = readU8(data, offset);
  let capabilityClassHash;
  [capabilityClassHash, offset] = readBytes(data, offset, 32);
  let softwareDigest;
  [softwareDigest, offset] = readBytes(data, offset, 32);
  return {
    bump,
    authority: new PublicKey(authority),
    stakeVault: new PublicKey(stakeVault),
    lockedStake,
    activeClaims,
    registeredAt,
    status,
    role,
    capabilityClassHash,
    softwareDigest
  };
}

function readOption(buffer, offset, readSome) {
  if (offset >= buffer.length) {
    return [null, offset];
  }
  const [tag, valueOffset] = readU8(buffer, offset);
  if (tag === 0) {
    return [null, valueOffset];
  }
  if (tag !== 1) {
    throw new Error(`invalid option tag: ${tag}`);
  }
  return readSome(buffer, valueOffset);
}

function readOptionPubkey(buffer, offset) {
  return readOption(buffer, offset, (innerBuffer, innerOffset) => {
    const [value, nextOffset] = readBytes(innerBuffer, innerOffset, 32);
    return [new PublicKey(value), nextOffset];
  });
}

function readOptionBytes32(buffer, offset) {
  return readOption(buffer, offset, (innerBuffer, innerOffset) => readBytes(innerBuffer, innerOffset, 32));
}

function readOptionString(buffer, offset) {
  return readOption(buffer, offset, readString);
}

function readOptionI64(buffer, offset) {
  return readOption(buffer, offset, readI64);
}

export function decodeJob(data) {
  validateDiscriminator(data, "Job");
  let offset = 8;
  let bump;
  [bump, offset] = readU8(data, offset);
  let nonce;
  [nonce, offset] = readU64(data, offset);
  let customer;
  [customer, offset] = readBytes(data, offset, 32);
  let worker;
  [worker, offset] = readBytes(data, offset, 32);
  let status;
  [status, offset] = readU8(data, offset);
  let rewardAmount;
  [rewardAmount, offset] = readU64(data, offset);
  let requiredStake;
  [requiredStake, offset] = readU64(data, offset);
  let jobClass;
  [jobClass, offset] = readU8(data, offset);
  let requiredRole;
  [requiredRole, offset] = readU8(data, offset);
  let requiredTier;
  [requiredTier, offset] = readU8(data, offset);
  let requiredCapabilityClassHash;
  [requiredCapabilityClassHash, offset] = readBytes(data, offset, 32);
  let requiredSoftwareDigest;
  [requiredSoftwareDigest, offset] = readBytes(data, offset, 32);
  let createdAt;
  [createdAt, offset] = readI64(data, offset);
  let claimDeadline;
  [claimDeadline, offset] = readI64(data, offset);
  let executionWindowSeconds;
  [executionWindowSeconds, offset] = readU32(data, offset);
  let executeDeadline;
  [executeDeadline, offset] = readI64(data, offset);
  let challengeWindowSeconds;
  [challengeWindowSeconds, offset] = readU32(data, offset);
  let challengeDeadline;
  [challengeDeadline, offset] = readI64(data, offset);
  let challengeBond;
  [challengeBond, offset] = readU64(data, offset);
  let challenger;
  [challenger, offset] = readBytes(data, offset, 32);
  let slashSettled;
  [slashSettled, offset] = readU8(data, offset);
  let escrowRefunded;
  [escrowRefunded, offset] = readU8(data, offset);
  let verifierRewardPaid;
  [verifierRewardPaid, offset] = readU8(data, offset);
  let customerSlashPaid;
  [customerSlashPaid, offset] = readU8(data, offset);
  let inputBundleHash;
  [inputBundleHash, offset] = readBytes(data, offset, 32);
  let expectedResultHash;
  [expectedResultHash, offset] = readBytes(data, offset, 32);
  let submittedResultHash;
  [submittedResultHash, offset] = readBytes(data, offset, 32);
  let inputCid;
  [inputCid, offset] = readString(data, offset);
  let outputCid;
  [outputCid, offset] = readString(data, offset);
  let resultBytes;
  [resultBytes, offset] = readVec(data, offset);
  let verifierAuthority;
  [verifierAuthority, offset] = readOptionPubkey(data, offset);
  let verifierAttestationHash;
  [verifierAttestationHash, offset] = readOptionBytes32(data, offset);
  let verifierEvidenceCid;
  [verifierEvidenceCid, offset] = readOptionString(data, offset);
  let verifierAttestationUnix;
  [verifierAttestationUnix, offset] = readOptionI64(data, offset);
  let assignedVerifierAuthority;
  [assignedVerifierAuthority, offset] = readOptionPubkey(data, offset);
  let assignedVerifierUnix;
  [assignedVerifierUnix, offset] = readOptionI64(data, offset);
  let reassignmentCounter;
  [reassignmentCounter, offset] = readU8(data, offset);
  return {
    bump,
    nonce,
    customer: new PublicKey(customer),
    worker: new PublicKey(worker),
    status,
    rewardAmount,
    requiredStake,
    jobClass,
    requiredRole,
    requiredTier,
    requiredCapabilityClassHash,
    requiredSoftwareDigest,
    createdAt,
    claimDeadline,
    executionWindowSeconds,
    executeDeadline,
    challengeWindowSeconds,
    challengeDeadline,
    challengeBond,
    challenger: new PublicKey(challenger),
    slashSettled: Boolean(slashSettled),
    escrowRefunded: Boolean(escrowRefunded),
    verifierRewardPaid: Boolean(verifierRewardPaid),
    customerSlashPaid: Boolean(customerSlashPaid),
    inputBundleHash,
    expectedResultHash,
    submittedResultHash,
    inputCid,
    outputCid,
    resultBytes,
    verifierAuthority,
    verifierAttestationHash,
    verifierEvidenceCid,
    verifierAttestationUnix,
    assignedVerifierAuthority,
    assignedVerifierUnix,
    reassignmentCounter
  };
}

export async function fetchProgramAccount(connection, publicKey, decoder) {
  const accountInfo = await connection.getAccountInfo(publicKey, "confirmed");
  if (!accountInfo) {
    return null;
  }
  return decoder(accountInfo.data);
}

export async function fetchAllJobs(connection) {
  const accounts = await connection.getProgramAccounts(PROGRAM_ID, { commitment: "confirmed" });
  return accounts
    .filter(({ account }) => account.data.subarray(0, 8).equals(anchorAccountDiscriminator("Job")))
    .map(({ pubkey, account }) => ({ publicKey: pubkey, job: decodeJob(account.data) }));
}

export async function fetchJob(connection, jobAddress) {
  const publicKey = new PublicKey(jobAddress);
  const job = await fetchProgramAccount(connection, publicKey, decodeJob);
  return job ? { publicKey, job } : null;
}

export async function fetchWorker(connection, workerAddress) {
  return fetchProgramAccount(connection, new PublicKey(workerAddress), decodeWorker);
}

export function settleJobIx({
  caller,
  config,
  paymentMint,
  tokenProgramId,
  job,
  worker,
  workerAuthority,
  jobEscrowVault,
  workerPaymentAccount
}) {
  return new TransactionInstruction({
    programId: PROGRAM_ID,
    data: encodeSettleJob(),
    keys: [
      { pubkey: caller, isSigner: true, isWritable: true },
      { pubkey: config, isSigner: false, isWritable: false },
      { pubkey: paymentMint, isSigner: false, isWritable: false },
      { pubkey: job, isSigner: false, isWritable: true },
      { pubkey: worker, isSigner: false, isWritable: true },
      { pubkey: workerAuthority, isSigner: false, isWritable: false },
      { pubkey: jobEscrowVault, isSigner: false, isWritable: true },
      { pubkey: workerPaymentAccount, isSigner: false, isWritable: true },
      { pubkey: requireTokenProgramId(tokenProgramId), isSigner: false, isWritable: false }
    ]
  });
}

export function refundSlashedJobEscrowIx({
  caller,
  config,
  paymentMint,
  tokenProgramId,
  job,
  customerAuthority,
  customerPaymentAccount,
  jobEscrowVault
}) {
  return new TransactionInstruction({
    programId: PROGRAM_ID,
    data: encodeRefundSlashedJobEscrow(),
    keys: [
      { pubkey: caller, isSigner: true, isWritable: true },
      { pubkey: config, isSigner: false, isWritable: false },
      { pubkey: paymentMint, isSigner: false, isWritable: false },
      { pubkey: job, isSigner: false, isWritable: true },
      { pubkey: customerAuthority, isSigner: false, isWritable: false },
      { pubkey: customerPaymentAccount, isSigner: false, isWritable: true },
      { pubkey: jobEscrowVault, isSigner: false, isWritable: true },
      { pubkey: requireTokenProgramId(tokenProgramId), isSigner: false, isWritable: false }
    ]
  });
}

export function claimVerifierSlashRewardIx({
  caller,
  config,
  paymentMint,
  tokenProgramId,
  job,
  verifierAuthority,
  verifierRewardAccount,
  worker,
  workerAuthority,
  workerStakeVault
}) {
  return new TransactionInstruction({
    programId: PROGRAM_ID,
    data: encodeClaimVerifierSlashReward(),
    keys: [
      { pubkey: caller, isSigner: true, isWritable: true },
      { pubkey: config, isSigner: false, isWritable: false },
      { pubkey: paymentMint, isSigner: false, isWritable: false },
      { pubkey: job, isSigner: false, isWritable: true },
      { pubkey: verifierAuthority, isSigner: false, isWritable: false },
      { pubkey: verifierRewardAccount, isSigner: false, isWritable: true },
      { pubkey: worker, isSigner: false, isWritable: true },
      { pubkey: workerAuthority, isSigner: false, isWritable: false },
      { pubkey: workerStakeVault, isSigner: false, isWritable: true },
      { pubkey: requireTokenProgramId(tokenProgramId), isSigner: false, isWritable: false }
    ]
  });
}

export function claimCustomerSlashCompensationIx({
  caller,
  config,
  paymentMint,
  tokenProgramId,
  job,
  customerAuthority,
  customerPaymentAccount,
  worker,
  workerAuthority,
  workerStakeVault
}) {
  return new TransactionInstruction({
    programId: PROGRAM_ID,
    data: encodeClaimCustomerSlashCompensation(),
    keys: [
      { pubkey: caller, isSigner: true, isWritable: true },
      { pubkey: config, isSigner: false, isWritable: false },
      { pubkey: paymentMint, isSigner: false, isWritable: false },
      { pubkey: job, isSigner: false, isWritable: true },
      { pubkey: customerAuthority, isSigner: false, isWritable: false },
      { pubkey: customerPaymentAccount, isSigner: false, isWritable: true },
      { pubkey: worker, isSigner: false, isWritable: true },
      { pubkey: workerAuthority, isSigner: false, isWritable: false },
      { pubkey: workerStakeVault, isSigner: false, isWritable: true },
      { pubkey: requireTokenProgramId(tokenProgramId), isSigner: false, isWritable: false }
    ]
  });
}

export function withdrawUnlockedStakeIx({
  authority,
  config,
  paymentMint,
  tokenProgramId,
  worker,
  workerStakeVault,
  workerDestinationAccount,
  amount
}) {
  return new TransactionInstruction({
    programId: PROGRAM_ID,
    data: encodeWithdrawUnlockedStake(amount),
    keys: [
      { pubkey: authority, isSigner: true, isWritable: true },
      { pubkey: config, isSigner: false, isWritable: false },
      { pubkey: paymentMint, isSigner: false, isWritable: false },
      { pubkey: worker, isSigner: false, isWritable: true },
      { pubkey: workerStakeVault, isSigner: false, isWritable: true },
      { pubkey: workerDestinationAccount, isSigner: false, isWritable: true },
      { pubkey: requireTokenProgramId(tokenProgramId), isSigner: false, isWritable: false }
    ]
  });
}

export function cancelOpenJobIx({
  customer,
  config,
  paymentMint,
  tokenProgramId,
  job,
  jobEscrowVault,
  customerPaymentAccount
}) {
  return new TransactionInstruction({
    programId: PROGRAM_ID,
    data: encodeCancelOpenJob(),
    keys: [
      { pubkey: customer, isSigner: true, isWritable: true },
      { pubkey: config, isSigner: false, isWritable: false },
      { pubkey: paymentMint, isSigner: false, isWritable: false },
      { pubkey: job, isSigner: false, isWritable: true },
      { pubkey: jobEscrowVault, isSigner: false, isWritable: true },
      { pubkey: customerPaymentAccount, isSigner: false, isWritable: true },
      { pubkey: requireTokenProgramId(tokenProgramId), isSigner: false, isWritable: false }
    ]
  });
}

export function slashStaleJobIx({
  caller,
  config,
  paymentMint,
  tokenProgramId,
  job,
  customerAuthority,
  customerPaymentAccount,
  worker,
  workerAuthority,
  workerStakeVault,
  jobEscrowVault
}) {
  return new TransactionInstruction({
    programId: PROGRAM_ID,
    data: encodeSlashStaleJob(),
    keys: [
      { pubkey: caller, isSigner: true, isWritable: true },
      { pubkey: config, isSigner: false, isWritable: false },
      { pubkey: paymentMint, isSigner: false, isWritable: false },
      { pubkey: job, isSigner: false, isWritable: true },
      { pubkey: customerAuthority, isSigner: false, isWritable: false },
      { pubkey: customerPaymentAccount, isSigner: false, isWritable: true },
      { pubkey: worker, isSigner: false, isWritable: true },
      { pubkey: workerAuthority, isSigner: false, isWritable: false },
      { pubkey: workerStakeVault, isSigner: false, isWritable: true },
      { pubkey: jobEscrowVault, isSigner: false, isWritable: true },
      { pubkey: requireTokenProgramId(tokenProgramId), isSigner: false, isWritable: false }
    ]
  });
}

export function validateDiscriminator(data, accountName) {
  const expected = anchorAccountDiscriminator(accountName);
  if (!data.subarray(0, 8).equals(expected)) {
    throw new Error(`unexpected discriminator for ${accountName}`);
  }
}
