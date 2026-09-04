import { PublicKey } from "@solana/web3.js";
import {
  configPda,
  fetchAllJobs,
  fetchWorker,
  JOB_STATUS,
  PROGRAM_ID,
  tokenAta,
  workerPda
} from "../src/protocol.mjs";
import { loadRuntimeKeypair, readRuntimeConfig, runtimeConnection, runtimePaymentMint, runtimeTokenProgramId } from "../src/runtime.mjs";

const JOB_STATUS_LABEL = Object.fromEntries(
  Object.entries(JOB_STATUS).map(([label, value]) => [value, label])
);

async function getSolBalance(connection, publicKey) {
  const lamports = await connection.getBalance(publicKey, "confirmed");
  return Number(lamports) / 1_000_000_000;
}

async function getTokenBalance(connection, publicKey) {
  const accountInfo = await connection.getAccountInfo(publicKey, "confirmed");
  if (!accountInfo) {
    return null;
  }
  const balance = await connection.getTokenAccountBalance(publicKey, "confirmed");
  return {
    amount: balance.value.amount,
    uiAmountString: balance.value.uiAmountString,
    decimals: balance.value.decimals
  };
}

async function getRuntimeIdentity(name) {
  try {
    return loadRuntimeKeypair(name);
  } catch {
    return null;
  }
}

async function main() {
  const runtimeConfig = readRuntimeConfig();
  const connection = runtimeConnection(runtimeConfig);
  const paymentMint = runtimePaymentMint(runtimeConfig);
  const tokenProgramId = runtimeTokenProgramId(runtimeConfig);
  const [config] = await configPda(PROGRAM_ID);

  const identities = {
    admin: await getRuntimeIdentity("admin"),
    customer: await getRuntimeIdentity("customer"),
    verifier: await getRuntimeIdentity("verifier"),
    watcher: await getRuntimeIdentity("watcher"),
    worker: await getRuntimeIdentity("worker")
  };

  const accounts = {
    config: config.toBase58(),
    programId: PROGRAM_ID.toBase58(),
    paymentMint: paymentMint.toBase58(),
    tokenProgramId: tokenProgramId.toBase58(),
    paymentDecimals: runtimeConfig.paymentDecimals
  };

  const balances = {};

  for (const [name, keypair] of Object.entries(identities)) {
    if (!keypair) {
      continue;
    }
    const authority = keypair.publicKey;
    const authorityAta = tokenAta(paymentMint, authority, false, tokenProgramId);
    balances[name] = {
      authority: authority.toBase58(),
      sol: await getSolBalance(connection, authority),
      paymentAta: authorityAta.toBase58(),
      payment: await getTokenBalance(connection, authorityAta)
    };
  }

  if (identities.worker) {
    const [workerAccount] = await workerPda(identities.worker.publicKey, PROGRAM_ID);
    const workerState = await fetchWorker(connection, workerAccount);
    const stakeVault = tokenAta(paymentMint, workerAccount, true, tokenProgramId);
    balances.worker.workerAccount = workerAccount.toBase58();
    balances.worker.stakeVault = stakeVault.toBase58();
    balances.worker.stakeVaultPayment = await getTokenBalance(connection, stakeVault);
    balances.worker.workerState = workerState
      ? {
          authority: workerState.authority.toBase58(),
          activeClaims: workerState.activeClaims,
          capabilityClassHash: Buffer.from(workerState.capabilityClassHash).toString("hex"),
          lockedStake: workerState.lockedStake.toString(),
          registeredAt: workerState.registeredAt.toString(),
          role: workerState.role,
          softwareDigest: Buffer.from(workerState.softwareDigest).toString("hex"),
          status: workerState.status
        }
      : null;
  }

  if (identities.verifier) {
    const [verifierAccount] = await workerPda(identities.verifier.publicKey, PROGRAM_ID);
    const verifierState = await fetchWorker(connection, verifierAccount);
    const stakeVault = tokenAta(paymentMint, verifierAccount, true, tokenProgramId);
    balances.verifier.workerAccount = verifierAccount.toBase58();
    balances.verifier.stakeVault = stakeVault.toBase58();
    balances.verifier.stakeVaultPayment = await getTokenBalance(connection, stakeVault);
    balances.verifier.workerState = verifierState
      ? {
          authority: verifierState.authority.toBase58(),
          activeClaims: verifierState.activeClaims,
          capabilityClassHash: Buffer.from(verifierState.capabilityClassHash).toString("hex"),
          lockedStake: verifierState.lockedStake.toString(),
          registeredAt: verifierState.registeredAt.toString(),
          role: verifierState.role,
          softwareDigest: Buffer.from(verifierState.softwareDigest).toString("hex"),
          status: verifierState.status
        }
      : null;
  }

  const jobs = await fetchAllJobs(connection);
  const jobSummaries = await Promise.all(
    jobs.map(async ({ publicKey, job }) => {
      const escrowVault = tokenAta(paymentMint, publicKey, true, tokenProgramId);
      const escrowBalance = await getTokenBalance(connection, escrowVault);
      return {
        job: publicKey.toBase58(),
        customer: job.customer.toBase58(),
        worker: job.worker.toBase58(),
        status: JOB_STATUS_LABEL[job.status] || `unknown(${job.status})`,
        rewardAmount: job.rewardAmount.toString(),
        requiredStake: job.requiredStake.toString(),
        jobClass: job.jobClass,
        requiredRole: job.requiredRole,
        requiredTier: job.requiredTier,
        requiredCapabilityClassHash: Buffer.from(job.requiredCapabilityClassHash).toString("hex"),
        requiredSoftwareDigest: Buffer.from(job.requiredSoftwareDigest).toString("hex"),
        inputCid: job.inputCid,
        outputCid: job.outputCid,
        challenger: job.challenger.toBase58(),
        slashSettled: job.slashSettled,
        escrowRefunded: job.escrowRefunded,
        verifierRewardPaid: job.verifierRewardPaid,
        customerSlashPaid: job.customerSlashPaid,
        claimDeadline: job.claimDeadline.toString(),
        executeDeadline: job.executeDeadline.toString(),
        challengeDeadline: job.challengeDeadline.toString(),
        challengeBond: job.challengeBond.toString(),
        escrowVault: escrowVault.toBase58(),
        escrowBalance
      };
    })
  );

  console.log(
    JSON.stringify(
      {
        accounts,
        balances,
        jobs: jobSummaries,
        rpcUrl: runtimeConfig.rpcUrl
      },
      null,
      2
    )
  );
}

main().catch((error) => {
  console.error(error);
  process.exit(1);
});
