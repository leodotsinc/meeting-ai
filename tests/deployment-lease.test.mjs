import assert from "node:assert/strict";
import filesystem from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { spawnSync } from "node:child_process";
import { test } from "node:test";
import {
  DeploymentDrainingError,
  withDeploymentLease,
  withDeploymentLeaseRoute,
} from "../src/lib/server/deployment-lease.ts";

async function fixture(t) {
  const directory = await filesystem.realpath(await filesystem.mkdtemp(join(tmpdir(), "meeting-deploy-")));
  await filesystem.chmod(directory, 0o755);
  await filesystem.mkdir(join(directory, "leases"), { mode: 0o700 });
  const previous = { directory: process.env.DEPLOY_CONTROL_DIR, mode: process.env.NODE_ENV };
  process.env.DEPLOY_CONTROL_DIR = directory;
  process.env.NODE_ENV = "test";
  t.after(async () => {
    if (previous.directory === undefined) delete process.env.DEPLOY_CONTROL_DIR;
    else process.env.DEPLOY_CONTROL_DIR = previous.directory;
    if (previous.mode === undefined) delete process.env.NODE_ENV;
    else process.env.NODE_ENV = previous.mode;
    await filesystem.rm(directory, { recursive: true, force: true });
  });
  return directory;
}

function deferred() {
  let resolve;
  const promise = new Promise((done) => { resolve = done; });
  return { promise, resolve };
}

test("lease exists throughout admitted work and is removed after success", async (t) => {
  const directory = await fixture(t);
  const value = await withDeploymentLease(async () => {
    const names = await filesystem.readdir(join(directory, "leases"));
    assert.equal(names.length, 1);
    const path = join(directory, "leases", names[0]);
    assert.equal((await filesystem.stat(path)).mode & 0o777, 0o600);
    assert.equal(JSON.parse(await filesystem.readFile(path, "utf8")).pid, process.pid);
    return 42;
  });
  assert.equal(value, 42);
  assert.deepEqual(await filesystem.readdir(join(directory, "leases")), []);
});

test("exception cleanup removes only this invocation's lease", async (t) => {
  const directory = await fixture(t);
  await filesystem.writeFile(join(directory, "leases", "stale.lease"), "operator review required");
  const failure = new Error("callback failure");
  await assert.rejects(withDeploymentLease(() => { throw failure; }), (error) => error === failure);
  assert.deepEqual(await filesystem.readdir(join(directory, "leases")), ["stale.lease"]);
});

test("existing drain marker prevents the callback and creates no lease", async (t) => {
  const directory = await fixture(t);
  await filesystem.writeFile(join(directory, "draining"), "");
  let ran = false;
  await assert.rejects(withDeploymentLease(() => { ran = true; }), DeploymentDrainingError);
  assert.equal(ran, false);
  assert.deepEqual(await filesystem.readdir(join(directory, "leases")), []);
});

test("marker racing after exclusive creation is rechecked before any callback", async (t) => {
  const directory = await fixture(t);
  const originalOpen = filesystem.open;
  t.mock.method(filesystem, "open", async (...arguments_) => {
    const handle = await originalOpen(...arguments_);
    await filesystem.writeFile(join(directory, "draining"), "");
    return handle;
  });
  let ran = false;
  await assert.rejects(withDeploymentLease(() => { ran = true; }), DeploymentDrainingError);
  assert.equal(ran, false);
  assert.deepEqual(await filesystem.readdir(join(directory, "leases")), []);
});

test("detached child retains admission after the parent HTTP work returns", async (t) => {
  const directory = await fixture(t);
  const finish = deferred();
  let child;
  await withDeploymentLease(async () => {
    child = withDeploymentLease(async () => {
      await finish.promise;
      return "finished";
    });
  });
  assert.equal((await filesystem.readdir(join(directory, "leases"))).length, 1);
  await filesystem.writeFile(join(directory, "draining"), "");
  await assert.rejects(withDeploymentLease(() => "new work"), DeploymentDrainingError);
  finish.resolve();
  assert.equal(await child, "finished");
  assert.deepEqual(await filesystem.readdir(join(directory, "leases")), []);
});

test("independent concurrent requests retain independent leases", async (t) => {
  const directory = await fixture(t);
  const startedA = deferred();
  const startedB = deferred();
  const finishA = deferred();
  const finishB = deferred();
  const a = withDeploymentLease(async () => { startedA.resolve(); await finishA.promise; });
  const b = withDeploymentLease(async () => { startedB.resolve(); await finishB.promise; });
  await Promise.all([startedA.promise, startedB.promise]);
  assert.equal((await filesystem.readdir(join(directory, "leases"))).length, 2);
  finishA.resolve();
  await a;
  assert.equal((await filesystem.readdir(join(directory, "leases"))).length, 1);
  finishB.resolve();
  await b;
  assert.deepEqual(await filesystem.readdir(join(directory, "leases")), []);
});

test("absent configuration passes locally but fails closed in production", async (t) => {
  await fixture(t);
  delete process.env.DEPLOY_CONTROL_DIR;
  assert.equal(await withDeploymentLease(() => "local"), "local");
  process.env.NODE_ENV = "production";
  await assert.rejects(withDeploymentLease(() => "unsafe"), DeploymentDrainingError);
});

test("configured missing or relative directory fails closed", async (t) => {
  const directory = await fixture(t);
  for (const target of [join(directory, "missing"), "relative/path"]) {
    process.env.DEPLOY_CONTROL_DIR = target;
    await assert.rejects(withDeploymentLease(() => "unsafe"), DeploymentDrainingError);
  }
});

test("unsafe permissions and symlink lease directory are rejected", async (t) => {
  const directory = await fixture(t);
  await filesystem.chmod(directory, 0o777);
  await assert.rejects(withDeploymentLease(() => "unsafe"), DeploymentDrainingError);
  await filesystem.chmod(directory, 0o755);
  await filesystem.chmod(join(directory, "leases"), 0o755);
  await assert.rejects(withDeploymentLease(() => "unsafe"), DeploymentDrainingError);
  await filesystem.rmdir(join(directory, "leases"));
  await filesystem.mkdir(join(directory, "other"), { mode: 0o700 });
  await filesystem.symlink(join(directory, "other"), join(directory, "leases"));
  await assert.rejects(withDeploymentLease(() => "unsafe"), DeploymentDrainingError);
});

test("non-processing route wrapper preserves arguments and returns retryable 503 during drain", async (t) => {
  const directory = await fixture(t);
  let calls = 0;
  const handler = withDeploymentLeaseRoute(async (request, context) => {
    calls += 1;
    assert.equal(request, "request fixture");
    assert.equal(context.id, "fixture");
    return Response.json({ created: true }, { status: 201 });
  });
  assert.equal((await handler("request fixture", { id: "fixture" })).status, 201);
  await filesystem.writeFile(join(directory, "draining"), "");
  const response = await handler("request fixture", { id: "fixture" });
  assert.equal(response.status, 503);
  assert.equal(response.headers.get("retry-after"), "60");
  assert.equal(response.headers.get("cache-control"), "no-store");
  assert.deepEqual(await response.json(), { error: "Service temporarily unavailable for maintenance" });
  assert.equal(calls, 1);
});

test("process crash leaves a lease for explicit operator review", async (t) => {
  const directory = await fixture(t);
  const moduleUrl = new URL("../src/lib/server/deployment-lease.ts", import.meta.url).href;
  const script = `import { withDeploymentLease } from ${JSON.stringify(moduleUrl)}; await withDeploymentLease(() => process.exit(9));`;
  const child = spawnSync(process.execPath, ["--experimental-strip-types", "--input-type=module", "-e", script], {
    env: { ...process.env, NODE_ENV: "test", DEPLOY_CONTROL_DIR: directory },
    encoding: "utf8",
    timeout: 10_000,
  });
  assert.equal(child.status, 9, child.stderr);
  const names = await filesystem.readdir(join(directory, "leases"));
  assert.equal(names.length, 1);
  await withDeploymentLease(() => "subsequent safe request");
  assert.deepEqual(await filesystem.readdir(join(directory, "leases")), names);
});
