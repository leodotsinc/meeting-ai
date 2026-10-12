import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import { mkdtempSync, mkdirSync, rmSync, symlinkSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, relative, resolve } from 'node:path';
import { test } from 'node:test';

// Exercise the exact transitive implementation selected for Prisma's config
// loader, not a separate dev dependency. Inputs are synthetic and local.
const require = createRequire(import.meta.url);
const prismaRequire = createRequire(require.resolve('@prisma/config'));
const { deepmerge } = prismaRequire('deepmerge-ts');

test('Prisma config merge retains ordinary nested record semantics', () => {
  const base = { schema: 'prisma/schema.prisma', migrations: { path: 'prisma/migrations' } };
  const override = { datasource: { url: 'postgresql://fixture:fixture@localhost/test' } };
  assert.deepEqual(deepmerge(base, override), { ...base, ...override });
  assert.deepEqual(base, { schema: 'prisma/schema.prisma', migrations: { path: 'prisma/migrations' } });
});

test('CVE-2026-40345 recursive objects do not exhaust the stack', () => {
  const left = { label: 'left' };
  left.self = left;
  const right = { label: 'right' };
  right.self = right;
  assert.doesNotThrow(() => deepmerge(left, right));
});

test('Next ESLint root discovery preserves literal, glob and array directory settings', (t) => {
  const root = mkdtempSync(join(tmpdir(), 'meeting-eslint-glob-'));
  t.after(() => rmSync(root, { recursive: true, force: true }));
  for (const directory of ['app-a/nested', 'app-b', '.hidden']) {
    mkdirSync(join(root, directory), { recursive: true });
  }
  writeFileSync(join(root, 'app-file'), 'fixture');
  symlinkSync(join(root, 'app-a'), join(root, 'linked-app'), 'dir');
  const nextRequire = createRequire(require.resolve('@next/eslint-plugin-next'));
  const { getRootDirs } = nextRequire('./utils/get-root-dirs');
  const discover = (rootDir) => getRootDirs({ cwd: root, settings: { next: { rootDir } } }).sort();

  assert.deepEqual(discover(undefined), [root]);
  assert.deepEqual(discover(root), [root]);
  assert.deepEqual(discover(join(root, 'app-*')), [join(root, 'app-a'), join(root, 'app-b')]);
  assert.deepEqual(discover(join(root, '{app-a,app-b}')), [join(root, 'app-a'), join(root, 'app-b')]);
  assert.deepEqual(discover([join(root, 'app-a'), join(root, 'app-b'), 42]),
    [join(root, 'app-a'), join(root, 'app-b')]);
  assert.deepEqual(discover(join(root, '*')), [join(root, 'app-a'), join(root, 'app-b'), join(root, 'linked-app')]);
  assert.deepEqual(discover(join(root, 'app-a/**')), [join(root, 'app-a/nested')]);
  assert.deepEqual(discover(join(root, 'linked-app')), [join(root, 'linked-app')]);
  assert.deepEqual(discover(relative(process.cwd(), join(root, 'app-*'))).map((p) => resolve(p)),
    [join(root, 'app-a'), join(root, 'app-b')]);
  assert.deepEqual(discover(join(root, 'missing*')), []);
  assert.deepEqual(discover('!' + join(root, 'app-a')), []);
  assert.throws(() => discover(join(root, '!(app-b)')), /explicit Next ESLint root directories/);
  assert.deepEqual(discover(join(root, 'app-a').replaceAll('/', '\\')), [join(root, 'app-a')]);
});
