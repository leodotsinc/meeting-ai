import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { readOnlyUrl, readMigrationFiles, validateHistory, validateCatalog, sha256 } from "./migration-check-lib.mjs";
import { parseArgs } from "./check-migrations.mjs";
import { validateQualificationUrl } from "./qualify-migrations.mjs";

const file = { name: "0_init", checksum: "a".repeat(64) };
const next = { name: "20260915_add_example", checksum: "b".repeat(64) };
const done = { migration_name: file.name, checksum: file.checksum, finished_at: "2026-09-14T00:00:00Z", rolled_back_at: null };
const rejects = (code, fn) => assert.throws(fn, (error) => error.code === code);

test("read-only URL preserves owner startup setting and enforces timeouts", () => {
  const result = new URL(readOnlyUrl("postgresql://migration:fixture@127.0.0.1/meeting_ai?options=-c%20role%3Dmeeting_owner"));
  assert.match(result.searchParams.get("options"), /role=meeting_owner.*default_transaction_read_only=on.*statement_timeout=15000/);
});
test("invalid database input fails before connection", () => {
  rejects("DATABASE_URL_REQUIRED", () => readOnlyUrl(undefined));
  rejects("POSTGRES_URL_REQUIRED", () => readOnlyUrl("https://example.test/db"));
});
test("CLI requires explicit identity and rejects duplicate flags", () => {
  rejects("EXPLICIT_IDENTITY_ARGUMENTS_REQUIRED", () => parseArgs(["--mode", "verify"]));
  rejects("INVALID_ARGUMENTS", () => parseArgs(["--mode", "verify", "--mode", "baseline"]));
  assert.equal(parseArgs(["--mode", "verify", "--expected-user", "meeting_migrator", "--expected-owner", "meeting_owner", "--expected-database", "meeting_ai"]).expected.owner, "meeting_owner");
});
test("verified history must be complete and baseline must be empty", () => {
  assert.deepEqual(validateHistory([done], [file], "verify"), { applied: [file.name], pending: [] });
  rejects("PENDING_MIGRATIONS", () => validateHistory([], [file], "verify"));
  rejects("BASELINE_REQUIRES_EMPTY_HISTORY", () => validateHistory([done], [file], "baseline"));
});
test("preflight accepts pending only after an applied prefix", () => {
  assert.deepEqual(validateHistory([done], [file, next], "preflight").pending, [next.name]);
  rejects("BASELINE_REGISTRATION_REQUIRED", () => validateHistory([], [file], "preflight"));
});
test("modified migration bytes block a release", () => {
  rejects("APPLIED_MIGRATION_CHECKSUM_MISMATCH", () => validateHistory([{ ...done, checksum: "c".repeat(64) }], [file], "verify"));
});
test("foreign, duplicate, unfinished, rolled back and nonprefix histories block", () => {
  rejects("UNKNOWN_APPLIED_MIGRATION", () => validateHistory([{ ...done, migration_name: "unknown" }], [file], "verify"));
  rejects("DUPLICATE_MIGRATION_HISTORY", () => validateHistory([done, done], [file], "verify"));
  rejects("UNFINISHED_MIGRATION", () => validateHistory([{ ...done, finished_at: null }], [file], "verify"));
  rejects("ROLLED_BACK_HISTORY_REQUIRES_REVIEW", () => validateHistory([{ ...done, rolled_back_at: done.finished_at }], [file], "verify"));
  rejects("NON_PREFIX_MIGRATION_HISTORY", () => validateHistory([{ ...done, migration_name: next.name, checksum: next.checksum }], [file, next], "preflight"));
});
test("frozen baseline SQL and original schema match their captured digests", () => {
  const root = resolve(import.meta.dirname, "..");
  const contract = JSON.parse(readFileSync(resolve(root, "scripts/migration-baseline.json")));
  assert.equal(readMigrationFiles(root)[0].checksum, contract.baseline_sql_sha256);
  // The original schema fixture is only expected while this first release has no later migrations.
  if (readMigrationFiles(root).length === 1) assert.equal(sha256(readFileSync(resolve(root, "prisma/schema.prisma"))), contract.baseline_schema_sha256);
});

const expected = { database: "meeting_ai", user: "meeting_migrator", owner: "meeting_owner" };
const contract = { baseline_tables: ["User"], baseline_enums: ["Language"], allowed_extensions: ["plpgsql"] };
function catalog() { return { identity: { database: expected.database, session_user: expected.user, current_user: expected.owner, read_only: "on", database_owner: expected.owner, schema_owner: expected.owner, rolsuper: false, rolcreatedb: false, rolcreaterole: false, rolreplication: false, rolbypassrls: false }, relations: [{ name: "User", kind: "r", owner: expected.owner }], enums: [{ name: "Language", owner: expected.owner }], extensions: ["plpgsql"], unsupported: [{ kind: "trigger", count: 0 }] }; }

test("catalog checks owner and caller, not Prisma's ignored ACL model alone", () => {
  validateCatalog(catalog(), expected, contract, "baseline");
  const changed = catalog(); changed.relations[0].owner = "admin";
  rejects("RELATION_OWNER_MISMATCH", () => validateCatalog(changed, expected, contract, "verify"));
  const elevated = catalog(); elevated.identity.rolsuper = true;
  rejects("MIGRATOR_GLOBAL_PRIVILEGE_REJECTED", () => validateCatalog(elevated, expected, contract, "verify"));
});
test("unsupported objects and unreviewed extensions block even if Prisma reports equality", () => {
  const trigger = catalog(); trigger.unsupported[0].count = 1;
  rejects("UNSUPPORTED_DATABASE_OBJECTS", () => validateCatalog(trigger, expected, contract, "verify"));
  const extension = catalog(); extension.extensions.push("unreviewed");
  rejects("UNREVIEWED_EXTENSION", () => validateCatalog(extension, expected, contract, "verify"));
});
test("baseline refuses an extra table despite otherwise valid ownership", () => {
  const extra = catalog(); extra.relations.push({ name: "Other", kind: "r", owner: expected.owner });
  rejects("BASELINE_TABLE_SET_MISMATCH", () => validateCatalog(extra, expected, contract, "baseline"));
});
test("mutating qualifier rejects real databases, hostnames, remote and option overrides", () => {
  assert.equal(validateQualificationUrl("postgresql://ci:fixture@127.0.0.1:5432/meeting_migrations_ci_test").hostname, "127.0.0.1");
  rejects("CI_LITERAL_LOOPBACK_REQUIRED", () => validateQualificationUrl("postgresql://ci:fixture@152.53.91.38/meeting_migrations_ci_test"));
  rejects("CI_LITERAL_LOOPBACK_REQUIRED", () => validateQualificationUrl("postgresql://ci:fixture@localhost/meeting_migrations_ci_test"));
  rejects("CI_DATABASE_NAME_REQUIRED", () => validateQualificationUrl("postgresql://ci:fixture@127.0.0.1/meeting_ai"));
  rejects("CI_URL_OPTIONS_REJECTED", () => validateQualificationUrl("postgresql://ci:fixture@127.0.0.1/meeting_migrations_ci_test?host=production"));
});
