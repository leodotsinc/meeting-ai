import { AsyncLocalStorage } from "node:async_hooks";
import { randomUUID } from "node:crypto";
import filesystem from "node:fs/promises";
import { isAbsolute, join, resolve } from "node:path";

interface Lease {
  path: string;
  references: number;
  released: boolean;
}

const currentLease = new AsyncLocalStorage<Lease>();

export class DeploymentDrainingError extends Error {
  constructor() {
    super("Service temporarily unavailable for maintenance");
    this.name = "DeploymentDrainingError";
  }
}

function isMissing(error: unknown): boolean {
  return error instanceof Error && "code" in error && error.code === "ENOENT";
}

async function drainMarkerExists(directory: string): Promise<boolean> {
  try {
    await filesystem.lstat(join(directory, "draining"));
    return true;
  } catch (error) {
    if (isMissing(error)) return false;
    throw new DeploymentDrainingError();
  }
}

async function removeOwnLease(path: string): Promise<void> {
  try {
    await filesystem.unlink(path);
  } catch (error) {
    if (!isMissing(error)) throw new DeploymentDrainingError();
  }
}

async function acquireLease(): Promise<Lease | null> {
  const directory = process.env.DEPLOY_CONTROL_DIR;
  const production = process.env.NODE_ENV === "production";
  if (!directory) {
    if (production) throw new DeploymentDrainingError();
    return null;
  }

  let createdPath: string | null = null;
  try {
    if (!isAbsolute(directory) || await filesystem.realpath(directory) !== resolve(directory)) {
      throw new DeploymentDrainingError();
    }
    const parent = await filesystem.lstat(directory);
    const leasesDirectory = join(directory, "leases");
    const leases = await filesystem.lstat(leasesDirectory);
    const applicationUid = process.getuid?.();
    const parentUid = production ? 0 : applicationUid;
    if (
      applicationUid === undefined ||
      !parent.isDirectory() || parent.isSymbolicLink() ||
      parent.uid !== parentUid || (parent.mode & 0o777) !== 0o755 ||
      !leases.isDirectory() || leases.isSymbolicLink() ||
      leases.uid !== applicationUid || (leases.mode & 0o777) !== 0o700
    ) {
      throw new DeploymentDrainingError();
    }
    if (await drainMarkerExists(directory)) throw new DeploymentDrainingError();

    const path = join(leasesDirectory, `${process.pid}-${randomUUID()}.lease`);
    const file = await filesystem.open(path, "wx", 0o600);
    createdPath = path;
    try {
      await file.writeFile(JSON.stringify({ pid: process.pid, started_at: new Date().toISOString() }));
    } finally {
      await file.close();
    }

    // The host may have enabled draining between the first check and creation.
    // It waits for this file; application writes cannot start before rechecking.
    if (await drainMarkerExists(directory)) throw new DeploymentDrainingError();
    return { path, references: 1, released: false };
  } catch {
    if (createdPath) await removeOwnLease(createdPath);
    throw new DeploymentDrainingError();
  }
}

async function releaseLease(lease: Lease): Promise<void> {
  lease.references -= 1;
  if (lease.references === 0) {
    lease.released = true;
    await removeOwnLease(lease.path);
  }
}

/** Hold one admission lease across nested work, including detached processing. */
export async function withDeploymentLease<T>(callback: () => T | Promise<T>): Promise<T> {
  const admitted = currentLease.getStore();
  if (admitted && !admitted.released) {
    // Increment before the first await: a fire-and-forget child retains admission
    // before its HTTP parent can return and release its own reference.
    admitted.references += 1;
    try {
      return await callback();
    } finally {
      await releaseLease(admitted);
    }
  }

  const lease = await acquireLease();
  if (!lease) return callback();
  return currentLease.run(lease, async () => {
    try {
      return await callback();
    } finally {
      await releaseLease(lease);
    }
  });
}

export function withDeploymentLeaseRoute<Arguments extends unknown[]>(
  handler: (...arguments_: Arguments) => Response | Promise<Response>,
): (...arguments_: Arguments) => Promise<Response> {
  return async (...arguments_: Arguments) => {
    try {
      return await withDeploymentLease(() => handler(...arguments_));
    } catch (error) {
      if (!(error instanceof DeploymentDrainingError)) throw error;
      return Response.json(
        { error: "Service temporarily unavailable for maintenance" },
        { status: 503, headers: { "Retry-After": "60", "Cache-Control": "no-store" } },
      );
    }
  };
}
