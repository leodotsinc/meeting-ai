# Next ESLint root directory adapter

The scoped `@next/eslint-plugin-next` override removes its `fast-glob` →
`micromatch` → `braces` chain because CVE-2026-93687 has no released fix.
The plugin's only consumer is `getRootDirs`, calling
`globSync(string, { onlyDirectories: true })`; this is the adapter's entire API.
It uses maintained `glob`, follows directory symlinks, preserves literal roots,
and excludes the parent for terminal `/**` patterns.

Meeting uses the default ESLint root (no `settings.next.rootDir`). Tests exercise
the actual installed Next caller with literals, absolute and relative paths,
arrays, braces, globstars, symlinks, missing paths, and explicit negation.
Negative extglobs such as `apps/!(mobile)` are rejected: the old matcher includes
hidden directories in that case while `glob` excludes them. Use explicit root
patterns if configuring such a monorepo. This adapter is not a general
replacement for the complete `fast-glob` API.
