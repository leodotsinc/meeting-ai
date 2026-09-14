import { checkMigrations, invariant } from "./migration-check-lib.mjs";

export function parseArgs(args) {
  const values = {};
  const allowed = ["mode", "expected-database", "expected-user", "expected-owner"];
  for (let index = 0; index < args.length; index += 2) {
    const key = args[index]?.replace(/^--/, "");
    invariant(args[index]?.startsWith("--") && allowed.includes(key) && !values[key] && args[index + 1], "INVALID_ARGUMENTS");
    values[key] = args[index + 1];
  }
  invariant(allowed.every((key) => values[key]), "EXPLICIT_IDENTITY_ARGUMENTS_REQUIRED");
  invariant(["baseline", "preflight", "verify"].includes(values.mode), "INVALID_MODE");
  invariant(allowed.slice(1).every((key) => /^[A-Za-z_][A-Za-z0-9_]{0,62}$/.test(values[key])), "INVALID_IDENTITY_ARGUMENTS");
  return { mode: values.mode, expected: { database: values["expected-database"], user: values["expected-user"], owner: values["expected-owner"] } };
}

if (process.argv[1] && import.meta.url === new URL(`file://${process.argv[1]}`).href) {
  try {
    const result = await checkMigrations({ ...parseArgs(process.argv.slice(2)), connectionString: process.env.DATABASE_URL });
    process.stdout.write(`${JSON.stringify(result)}\n`);
  } catch (error) {
    const code = /^[A-Z][A-Z0-9_]{2,80}$/.test(error.code || "") ? error.code : "MIGRATION_CHECK_FAILED";
    process.stderr.write(`${JSON.stringify({ ok: false, error: code })}\n`);
    process.exitCode = 1;
  }
}
