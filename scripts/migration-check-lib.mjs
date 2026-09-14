import { createHash } from "node:crypto";
import { readFileSync, readdirSync, lstatSync } from "node:fs";
import { resolve, join } from "node:path";
import { spawnSync } from "node:child_process";
import pg from "pg";

export function invariant(condition, code) {
  if (!condition) throw Object.assign(new Error(code), { code });
}

export const sha256 = (value) => createHash("sha256").update(value).digest("hex");

export function readOnlyUrl(value) {
  let url;
  try { url = new URL(value); } catch { invariant(false, "DATABASE_URL_REQUIRED"); }
  invariant(["postgres:", "postgresql:"].includes(url.protocol), "POSTGRES_URL_REQUIRED");
  invariant(url.username && url.hostname && url.pathname.length > 1, "DATABASE_URL_INCOMPLETE");
  // PostgreSQL startup settings apply to every introspection connection, including Prisma.
  // Appending preserves an existing role setting; the read-only settings win if duplicated.
  const existing = url.searchParams.get("options") || "";
  url.searchParams.set("options", `${existing} -c default_transaction_read_only=on -c statement_timeout=15000 -c lock_timeout=3000`.trim());
  return url.toString();
}

export function readMigrationFiles(root) {
  const directory = join(root, "prisma/migrations");
  const names = readdirSync(directory, { withFileTypes: true });
  invariant(names.every((entry) => !entry.isSymbolicLink()), "MIGRATION_SYMLINK_REJECTED");
  const files = names.filter((entry) => entry.isDirectory()).map((entry) => {
    invariant(/^[0-9][A-Za-z0-9_]*$/.test(entry.name), "INVALID_MIGRATION_NAME");
    const file = join(directory, entry.name, "migration.sql");
    invariant(lstatSync(file).isFile() && !lstatSync(file).isSymbolicLink(), "MIGRATION_FILE_REQUIRED");
    const sql = readFileSync(file);
    invariant(sql.length > 0, "EMPTY_MIGRATION_FILE");
    return { name: entry.name, checksum: sha256(sql) };
  }).sort((a, b) => a.name < b.name ? -1 : a.name > b.name ? 1 : 0);
  invariant(files.length > 0, "NO_VERSIONED_MIGRATIONS");
  return files;
}

export function validateHistory(rows, files, mode) {
  invariant(["baseline", "preflight", "verify"].includes(mode), "INVALID_MODE");
  invariant(rows.every((row) => row.rolled_back_at === null), "ROLLED_BACK_HISTORY_REQUIRES_REVIEW");
  invariant(rows.every((row) => row.finished_at !== null), "UNFINISHED_MIGRATION");
  const seen = new Set();
  for (const row of rows) {
    invariant(!seen.has(row.migration_name), "DUPLICATE_MIGRATION_HISTORY");
    seen.add(row.migration_name);
    const file = files.find((item) => item.name === row.migration_name);
    invariant(file, "UNKNOWN_APPLIED_MIGRATION");
    invariant(row.checksum === file.checksum, "APPLIED_MIGRATION_CHECKSUM_MISMATCH");
  }
  const applied = files.filter((file) => seen.has(file.name)).map((file) => file.name);
  invariant(applied.every((name, index) => files[index].name === name), "NON_PREFIX_MIGRATION_HISTORY");
  const pending = files.filter((file) => !seen.has(file.name)).map((file) => file.name);
  if (mode === "baseline") invariant(rows.length === 0, "BASELINE_REQUIRES_EMPTY_HISTORY");
  if (mode === "verify") invariant(pending.length === 0, "PENDING_MIGRATIONS");
  if (mode === "preflight") invariant(applied.length > 0, "BASELINE_REGISTRATION_REQUIRED");
  return { applied, pending };
}

// Prisma does not model all PostgreSQL objects. Fail closed on unsupported additions
// until their explicit migration/restore handling is reviewed and this policy changes.
export function validateCatalog(catalog, expected, contract, mode) {
  const identity = catalog.identity;
  invariant(identity.database === expected.database, "DATABASE_IDENTITY_MISMATCH");
  invariant(identity.session_user === expected.user, "SESSION_USER_MISMATCH");
  invariant([expected.user, expected.owner].includes(identity.current_user), "CURRENT_USER_MISMATCH");
  invariant(identity.read_only === "on", "READ_ONLY_CONNECTION_REQUIRED");
  invariant(!identity.rolsuper && !identity.rolcreatedb && !identity.rolcreaterole && !identity.rolreplication && !identity.rolbypassrls, "MIGRATOR_GLOBAL_PRIVILEGE_REJECTED");
  invariant(identity.database_owner === expected.owner && identity.schema_owner === expected.owner, "DATABASE_SCHEMA_OWNER_MISMATCH");
  invariant(catalog.relations.every((row) => row.owner === expected.owner), "RELATION_OWNER_MISMATCH");
  invariant(catalog.enums.every((row) => row.owner === expected.owner), "ENUM_OWNER_MISMATCH");
  invariant(catalog.extensions.every((name) => contract.allowed_extensions.includes(name)), "UNREVIEWED_EXTENSION");
  invariant(catalog.unsupported.every((item) => Number(item.count) === 0), "UNSUPPORTED_DATABASE_OBJECTS");
  if (mode === "baseline") {
    const tables = catalog.relations.filter((row) => row.kind === "r" && row.name !== "_prisma_migrations").map((row) => row.name).sort();
    invariant(JSON.stringify(tables) === JSON.stringify(contract.baseline_tables), "BASELINE_TABLE_SET_MISMATCH");
    invariant(JSON.stringify(catalog.enums.map((row) => row.name).sort()) === JSON.stringify(contract.baseline_enums), "BASELINE_ENUM_SET_MISMATCH");
  }
}

export async function inspectCatalog(connectionString) {
  const client = new pg.Client({ connectionString, application_name: "meeting-migration-check", connectionTimeoutMillis: 10000 });
  await client.connect();
  try {
    await client.query("BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY");
    const identity = (await client.query(`SELECT current_database() AS database, session_user, current_user,
      current_setting('default_transaction_read_only') AS read_only, r.rolsuper,r.rolcreatedb,r.rolcreaterole,r.rolreplication,r.rolbypassrls,
      pg_get_userbyid(d.datdba) AS database_owner, pg_get_userbyid(n.nspowner) AS schema_owner
      FROM pg_roles r CROSS JOIN pg_database d CROSS JOIN pg_namespace n
      WHERE r.rolname=session_user AND d.datname=current_database() AND n.nspname='public'`)).rows[0];
    const relations = (await client.query(`SELECT c.relname AS name,c.relkind AS kind,pg_get_userbyid(c.relowner) AS owner,
      c.relrowsecurity,c.relforcerowsecurity,c.relacl::text AS acl
      FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
      WHERE n.nspname='public' AND c.relkind IN ('r','p','v','m','f','S') ORDER BY c.relname`)).rows;
    const enums = (await client.query(`SELECT t.typname AS name,pg_get_userbyid(t.typowner) AS owner,t.typacl::text AS acl,
      array_agg(e.enumlabel ORDER BY e.enumsortorder) AS values FROM pg_type t
      JOIN pg_namespace n ON n.oid=t.typnamespace JOIN pg_enum e ON e.enumtypid=t.oid
      WHERE n.nspname='public' GROUP BY t.oid,t.typname,t.typowner,t.typacl ORDER BY t.typname`)).rows;
    const extensions = (await client.query("SELECT extname FROM pg_extension ORDER BY extname")).rows.map((row) => row.extname);
    const unsupported = (await client.query(`SELECT 'relation_kind' AS kind,count(*)::int AS count FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='public' AND c.relkind IN ('p','v','m','f','S')
      UNION ALL SELECT 'rls',count(*)::int FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='public' AND (c.relrowsecurity OR c.relforcerowsecurity)
      UNION ALL SELECT 'trigger',count(*)::int FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='public' AND NOT t.tgisinternal
      UNION ALL SELECT 'rule',count(*)::int FROM pg_rewrite r JOIN pg_class c ON c.oid=r.ev_class JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='public'
      UNION ALL SELECT 'function',count(*)::int FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace WHERE n.nspname='public' AND NOT EXISTS(SELECT 1 FROM pg_depend d WHERE d.classid='pg_proc'::regclass AND d.objid=p.oid AND d.deptype='e')
      UNION ALL SELECT 'domain',count(*)::int FROM pg_type t JOIN pg_namespace n ON n.oid=t.typnamespace WHERE n.nspname='public' AND t.typtype='d'
      UNION ALL SELECT 'event_trigger',count(*)::int FROM pg_event_trigger
      UNION ALL SELECT 'column_acl',count(*)::int FROM pg_attribute a JOIN pg_class c ON c.oid=a.attrelid JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='public' AND a.attacl IS NOT NULL
      UNION ALL SELECT 'check_or_exclusion_constraint',count(*)::int FROM pg_constraint c JOIN pg_namespace n ON n.oid=c.connamespace WHERE n.nspname='public' AND c.contype IN ('c','x')
      UNION ALL SELECT 'deferrable_constraint',count(*)::int FROM pg_constraint c JOIN pg_namespace n ON n.oid=c.connamespace WHERE n.nspname='public' AND (c.condeferrable OR c.condeferred)
      UNION ALL SELECT 'partial_or_expression_index',count(*)::int FROM pg_index i JOIN pg_class c ON c.oid=i.indrelid JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='public' AND (i.indpred IS NOT NULL OR i.indexprs IS NOT NULL)
      UNION ALL SELECT 'generated_column',count(*)::int FROM pg_attribute a JOIN pg_class c ON c.oid=a.attrelid JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='public' AND a.attgenerated<>''
      UNION ALL SELECT 'custom_schema',count(*)::int FROM pg_namespace n WHERE n.nspname NOT IN ('public','information_schema') AND n.nspname !~ '^pg_'
      ORDER BY kind`)).rows;
    const definitions = (await client.query(`SELECT 'column' AS kind,c.relname AS relation,a.attnum::text AS key,
      jsonb_build_object('name',a.attname,'type',format_type(a.atttypid,a.atttypmod),'notnull',a.attnotnull,'identity',a.attidentity,'generated',a.attgenerated,'default',pg_get_expr(d.adbin,d.adrelid),'collation',a.attcollation::regcollation::text) AS definition
      FROM pg_attribute a JOIN pg_class c ON c.oid=a.attrelid JOIN pg_namespace n ON n.oid=c.relnamespace LEFT JOIN pg_attrdef d ON d.adrelid=a.attrelid AND d.adnum=a.attnum
      WHERE n.nspname='public' AND c.relkind='r' AND a.attnum>0 AND NOT a.attisdropped
      UNION ALL SELECT 'constraint',c.relname,k.conname,to_jsonb(pg_get_constraintdef(k.oid,true)) FROM pg_constraint k JOIN pg_class c ON c.oid=k.conrelid JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='public'
      UNION ALL SELECT 'index',c.relname,i.relname,jsonb_build_object('definition',pg_get_indexdef(x.indexrelid),'valid',x.indisvalid,'ready',x.indisready) FROM pg_index x JOIN pg_class c ON c.oid=x.indrelid JOIN pg_class i ON i.oid=x.indexrelid JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='public'
      ORDER BY kind,relation,key`)).rows;
    const hasHistory = relations.some((row) => row.name === "_prisma_migrations" && row.kind === "r");
    const history = hasHistory ? (await client.query('SELECT migration_name,checksum,finished_at,rolled_back_at FROM public."_prisma_migrations" ORDER BY migration_name,started_at')).rows : [];
    await client.query("ROLLBACK");
    return { identity, relations, enums, extensions, unsupported, definitions, hasHistory, history };
  } finally { await client.end(); }
}

export function runPrisma(root, args, connectionString, { readOnly = false } = {}) {
  const env = { ...process.env, DATABASE_URL: readOnly ? readOnlyUrl(connectionString) : connectionString, CHECKPOINT_DISABLE: "1" };
  delete env.SHADOW_DATABASE_URL;
  const result = spawnSync(process.execPath, [join(root, "node_modules/prisma/build/index.js"), ...args], {
    cwd: root, env, encoding: "utf8", timeout: 120000, maxBuffer: 8 * 1024 * 1024,
  });
  // Never forward raw Prisma output: errors can contain the connection URI or SQL.
  invariant(!result.error && result.signal === null, "PRISMA_COMMAND_FAILED");
  return { status: result.status };
}

export async function checkMigrations({ root = resolve(import.meta.dirname, ".."), mode, expected, connectionString }) {
  const contract = JSON.parse(readFileSync(join(root, "scripts/migration-baseline.json"), "utf8"));
  const files = readMigrationFiles(root);
  invariant(files[0].name === contract.baseline && files[0].checksum === contract.baseline_sql_sha256, "BASELINE_FILE_CHANGED");
  if (mode === "baseline") {
    invariant(files.length === 1, "BASELINE_WITH_NEW_MIGRATIONS_REJECTED");
    invariant(sha256(readFileSync(join(root, "prisma/schema.prisma"))) === contract.baseline_schema_sha256, "BASELINE_SCHEMA_CHANGED");
  }
  const url = readOnlyUrl(connectionString);
  const before = await inspectCatalog(url);
  validateCatalog(before, expected, contract, mode);
  const history = validateHistory(before.history, files, mode);
  const checkDrift = mode !== "preflight" || history.pending.length === 0;
  if (checkDrift) {
    const diff = runPrisma(root, ["migrate", "diff", "--from-config-datasource", "--to-schema", "prisma/schema.prisma", "--exit-code"], url, { readOnly: true });
    invariant(diff.status !== 2, "SCHEMA_DRIFT");
    invariant(diff.status === 0, "SCHEMA_DIFF_FAILED");
  }
  const after = await inspectCatalog(url);
  const catalogHash = sha256(JSON.stringify(before));
  invariant(catalogHash === sha256(JSON.stringify(after)), "CONCURRENT_SCHEMA_OR_HISTORY_CHANGE");
  return { ok: true, mode, database: expected.database, session_user: expected.user,
    owner: expected.owner, baseline: contract.baseline, ...history,
    schema_drift_checked: checkDrift, catalog_sha256: catalogHash,
    history_present: before.hasHistory, baseline_eligible: mode === "baseline",
    note: checkDrift ? "Schema and catalog checked; no writes performed." : "History prefix checked only; pending migration drift requires isolated qualification and policy approval." };
}
