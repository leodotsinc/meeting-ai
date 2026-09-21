import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
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
