// Writes only to an explicitly disposable, empty, loopback CI database.
// This is migration qualification, not a production-data restore test.
import { randomBytes } from "node:crypto";
import { resolve } from "node:path";
import pg from "pg";
import { checkMigrations, invariant, runPrisma, readOnlyUrl } from "./migration-check-lib.mjs";

export function validateQualificationUrl(value) {
  let url;
  try { url = new URL(value); } catch { invariant(false, "CI_DATABASE_URL_REQUIRED"); }
  invariant(["postgres:", "postgresql:"].includes(url.protocol), "CI_POSTGRES_URL_REQUIRED");
  invariant(["127.0.0.1", "[::1]"].includes(url.hostname), "CI_LITERAL_LOOPBACK_REQUIRED");
  invariant(/^\/meeting_migrations_ci_[a-z0-9_]{4,28}$/.test(url.pathname), "CI_DATABASE_NAME_REQUIRED");
  invariant(!url.search && !url.hash, "CI_URL_OPTIONS_REJECTED");
  return url;
}

async function expectFailure(action, expectedCode) {
  let failed = false;
  try { await action(); } catch (error) {
    invariant(error.code === expectedCode, "UNEXPECTED_QUALIFICATION_FAILURE");
    failed = true;
  }
  invariant(failed, "NEGATIVE_QUALIFICATION_DID_NOT_FAIL");
}

export async function qualifyMigrations(value, root = resolve(import.meta.dirname, "..")) {
  const url = validateQualificationUrl(value);
  const database = url.pathname.slice(1);
  const token = database.replace("meeting_migrations_ci_", "");
  const owner = `meeting_ci_${token}_owner`;
  const migrator = `meeting_ci_${token}_migrator`;
  const runtime = `meeting_ci_${token}_runtime`;
  const password = randomBytes(24).toString("hex");
  const runtimePassword = randomBytes(24).toString("hex");
  const admin = new pg.Client({ connectionString: url.toString(), connectionTimeoutMillis: 10000 });
  await admin.connect();
  try {
    const identity = (await admin.query("SELECT current_database() AS name,inet_server_addr()::text AS address,(SELECT rolsuper FROM pg_roles WHERE rolname=current_user) AS superuser")).rows[0];
    // The server sees the container address with Docker forwarding; the URL itself must be literal loopback.
    invariant(identity.name === database && identity.superuser, "CI_ADMIN_IDENTITY_REQUIRED");
    const objects = (await admin.query("SELECT count(*)::int AS count FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='public' AND c.relkind IN ('r','p','v','m','f','S')")).rows[0].count;
    invariant(objects === 0, "CI_DATABASE_MUST_BE_EMPTY");
    const existing = await admin.query("SELECT rolname FROM pg_roles WHERE rolname=ANY($1)", [[owner, migrator, runtime]]);
    invariant(existing.rowCount === 0, "CI_ROLES_ALREADY_EXIST");
    // Names are generated from the restricted database suffix; passwords are random hex.
    await admin.query(`CREATE ROLE "${owner}" NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
      CREATE ROLE "${migrator}" LOGIN INHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS PASSWORD '${password}';
      CREATE ROLE "${runtime}" LOGIN NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS PASSWORD '${runtimePassword}';
      GRANT "${owner}" TO "${migrator}";
      ALTER DATABASE "${database}" OWNER TO "${owner}";
      ALTER SCHEMA public OWNER TO "${owner}";
      REVOKE ALL ON DATABASE "${database}" FROM PUBLIC;
      REVOKE ALL ON SCHEMA public FROM PUBLIC;
      GRANT CONNECT ON DATABASE "${database}" TO "${runtime}","${migrator}";
      GRANT USAGE ON SCHEMA public TO "${runtime}";
      ALTER DEFAULT PRIVILEGES FOR ROLE "${owner}" IN SCHEMA public GRANT SELECT,INSERT,UPDATE,DELETE ON TABLES TO "${runtime}";
      ALTER DEFAULT PRIVILEGES FOR ROLE "${owner}" IN SCHEMA public GRANT USAGE,SELECT ON SEQUENCES TO "${runtime}";
      ALTER DEFAULT PRIVILEGES FOR ROLE "${migrator}" IN SCHEMA public GRANT SELECT,INSERT,UPDATE,DELETE ON TABLES TO "${runtime}";`);
    const migrationUrl = new URL(url);
    migrationUrl.username = migrator;
    migrationUrl.password = password;
    migrationUrl.searchParams.set("options", `-c role=${owner}`);
    const expected = { database, user: migrator, owner };
    const check = (mode) => checkMigrations({ root, mode, expected, connectionString: migrationUrl.toString() });
    const migrate = (args) => {
      const result = runPrisma(root, ["migrate", ...args], migrationUrl.toString());
      invariant(result.status === 0, "ISOLATED_MIGRATION_COMMAND_FAILED");
    };
    migrate(["deploy"]);
    await admin.query(`REVOKE ALL ON public."_prisma_migrations" FROM "${runtime}"`);
    const fresh = await check("verify");
    const baselineChecksum = (await admin.query('SELECT checksum FROM public."_prisma_migrations" WHERE migration_name=$1', ["0_init"])).rows[0].checksum;
    const runtimeUrl = new URL(url);
    runtimeUrl.username = runtime;
    runtimeUrl.password = runtimePassword;
    const runtimeClient = new pg.Client({ connectionString: runtimeUrl.toString(), connectionTimeoutMillis: 10000 });
    await runtimeClient.connect();
    try {
      const identity = (await runtimeClient.query("SELECT session_user,current_user")).rows[0];
      invariant(identity.session_user === runtime && identity.current_user === runtime, "RUNTIME_LOGIN_IDENTITY_MISMATCH");
      await runtimeClient.query(`INSERT INTO public."User"(id,email,"passwordHash","updatedAt") VALUES ('ci-migration-user','migration-fixture@example.invalid','deliberately-not-a-login-hash',NOW())`);
      await runtimeClient.query(`UPDATE public."User" SET email='migration-fixture-updated@example.invalid' WHERE id='ci-migration-user'`);
      const fixture = (await runtimeClient.query(`SELECT id FROM public."User" WHERE id='ci-migration-user'`)).rowCount;
      invariant(fixture === 1, "RUNTIME_DML_FIXTURE_MISSING");
      await expectFailure(() => runtimeClient.query('SELECT * FROM public."_prisma_migrations"'), "42501");
      await expectFailure(() => runtimeClient.query('CREATE TABLE public."RuntimeMustNotCreate"(id integer)'), "42501");
      await expectFailure(() => runtimeClient.query(`SET ROLE "${owner}"`), "42501");
    } finally { await runtimeClient.end(); }
    const readOnly = new pg.Client({ connectionString: readOnlyUrl(migrationUrl.toString()), connectionTimeoutMillis: 10000 });
    await readOnly.connect();
    try { await expectFailure(() => readOnly.query('CREATE TABLE public."ReadOnlyMustNotCreate"(id integer)'), "25006"); }
    finally { await readOnly.end(); }
    migrate(["deploy"]);
    await check("verify");
    // Emulate the existing db-push schema with empty migration metadata, preserving a row.
    // This mutation is strictly confined to the disposable CI database established above.
    await admin.query('DELETE FROM public."_prisma_migrations"');
    const adopt = await check("baseline");
    invariant(adopt.baseline_eligible, "BASELINE_ELIGIBILITY_FAILED");
    migrate(["resolve", "--applied", "0_init"]);
    await check("verify");
    await check("preflight");
    invariant((await admin.query(`SELECT id FROM public."User" WHERE id='ci-migration-user'`)).rowCount === 1, "BASELINE_CHANGED_APPLICATION_DATA");
    await expectFailure(() => check("baseline"), "BASELINE_REQUIRES_EMPTY_HISTORY");
    await admin.query('UPDATE public."_prisma_migrations" SET checksum=$1 WHERE migration_name=$2', ["0".repeat(64), "0_init"]);
    await expectFailure(() => check("verify"), "APPLIED_MIGRATION_CHECKSUM_MISMATCH");
    await admin.query('UPDATE public."_prisma_migrations" SET checksum=$1 WHERE migration_name=$2', [baselineChecksum, "0_init"]);
    await admin.query('ALTER TABLE public."User" ADD COLUMN "ci_drift_probe" text');
    await expectFailure(() => check("verify"), "SCHEMA_DRIFT");
    await admin.query('ALTER TABLE public."User" DROP COLUMN "ci_drift_probe"');
    await admin.query('ALTER TABLE public."User" ENABLE ROW LEVEL SECURITY');
    await expectFailure(() => check("verify"), "UNSUPPORTED_DATABASE_OBJECTS");
    await admin.query('ALTER TABLE public."User" DISABLE ROW LEVEL SECURITY');
    const final = await check("verify");
    await admin.query(`SET ROLE "${runtime}"`);
    await admin.query(`DELETE FROM public."User" WHERE id='ci-migration-user'`);
    await admin.query("RESET ROLE");
    return { ok: true, database, baseline: "0_init", migrations: fresh.applied.length,
      checks: ["fresh_deploy", "repeat_deploy", "separate_owner_migrator_runtime", "runtime_login", "runtime_crud", "runtime_history_denied", "runtime_ddl_denied", "runtime_owner_escalation_denied", "readonly_connection_enforced", "baseline_adoption_preserves_fixture", "repeat_baseline_denied", "checksum_change_denied", "schema_drift_denied", "rls_denied", "final_schema_verified"],
      final_catalog_sha256: final.catalog_sha256, production_restore_test: false };
  } finally { await admin.end(); }
}

if (process.argv[1] && import.meta.url === new URL(`file://${process.argv[1]}`).href) {
  try { process.stdout.write(`${JSON.stringify(await qualifyMigrations(process.env.DATABASE_URL))}\n`); }
  catch (error) {
    const code = /^[A-Z][A-Z0-9_]{2,80}$/.test(error.code || "") ? error.code : "MIGRATION_QUALIFICATION_FAILED";
    process.stderr.write(`${JSON.stringify({ ok: false, error: code })}\n`);
    process.exitCode = 1;
  }
}
