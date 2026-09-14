import assert from "node:assert/strict";
import { readFile, readdir } from "node:fs/promises";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { test } from "node:test";
import ts from "typescript";

const MUTATIONS = new Set(["POST", "PUT", "PATCH", "DELETE"]);

function inspectRoute(source) {
  const ast = ts.createSourceFile("route.ts", source, ts.ScriptTarget.Latest, true);
  const covered = [];
  const unguarded = [];
  for (const statement of ast.statements) {
    if (ts.isExportDeclaration(statement)) {
      if (!statement.exportClause) unguarded.push("wildcard export");
      else if (ts.isNamedExports(statement.exportClause)) {
        for (const element of statement.exportClause.elements) {
          if (MUTATIONS.has(element.name.text)) unguarded.push(element.name.text);
        }
      }
      continue;
    }
    if (!statement.modifiers?.some((modifier) => modifier.kind === ts.SyntaxKind.ExportKeyword)) continue;
    if (ts.isFunctionDeclaration(statement) && statement.name && MUTATIONS.has(statement.name.text)) {
      unguarded.push(statement.name.text);
    }
    if (!ts.isVariableStatement(statement)) continue;
    for (const declaration of statement.declarationList.declarations) {
      if (ts.isObjectBindingPattern(declaration.name)) {
        for (const element of declaration.name.elements) {
          if (ts.isIdentifier(element.name) && MUTATIONS.has(element.name.text)) unguarded.push(element.name.text);
        }
        continue;
      }
      if (!ts.isIdentifier(declaration.name) || !MUTATIONS.has(declaration.name.text)) continue;
      const initializer = declaration.initializer;
      if (initializer && ts.isCallExpression(initializer) && ts.isIdentifier(initializer.expression)
          && initializer.expression.text === "withDeploymentLeaseRoute") covered.push(declaration.name.text);
      else unguarded.push(declaration.name.text);
    }
  }
  return { covered, unguarded };
}

async function routePaths(directory) {
  const entries = await readdir(directory, { withFileTypes: true });
  const children = await Promise.all(entries.map(async (entry) => {
    const path = join(directory, entry.name);
    if (entry.isDirectory()) return routePaths(path);
    return entry.name === "route.ts" ? [path] : [];
  }));
  return children.flat();
}

test("coverage gate rejects unwrapped mutation declarations and re-exports", () => {
  for (const source of [
    "export async function POST() {}",
    "export const POST = handler;",
    "export const { GET, POST } = handlers;",
    "export { handler as DELETE };",
    "export * from './other';",
  ]) assert.notEqual(inspectRoute(source).unguarded.length, 0, source);
  assert.deepEqual(inspectRoute("export const POST = withDeploymentLeaseRoute(handler);"), {
    covered: ["POST"], unguarded: [],
  });
});

test("every mutating API export, including auth and internal upload, has admission control", async () => {
  const root = fileURLToPath(new URL("../src/app/api/", import.meta.url));
  const routes = await routePaths(root);
  let total = 0;
  for (const path of routes) {
    const { covered, unguarded } = inspectRoute(await readFile(path, "utf8"));
    assert.deepEqual(unguarded, [], path);
    total += covered.length;
  }
  assert.ok(total >= 17, "expected current mutation surface to be covered");
});
