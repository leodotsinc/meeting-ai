import { globSync as globDirectories, hasMagic } from 'glob';
import { statSync } from 'node:fs';
import { isAbsolute } from 'node:path';

// @next/eslint-plugin-next only calls globSync to discover root directories.
// This adapter deliberately exposes only that caller's interface.
export function globSync(pattern, options) {
  if (typeof pattern !== 'string' || options?.onlyDirectories !== true ||
      Object.keys(options).some((key) => key !== 'onlyDirectories')) {
    throw new TypeError('Unsupported Next ESLint root-directory glob call');
  }
  // The libraries disagree about hidden directories in negative extglobs.
  // Require explicit patterns if a future lint configuration needs this case.
  if (pattern.includes('!(')) throw new TypeError('Use explicit Next ESLint root directories instead of negative extglobs');
  if (pattern.startsWith('!')) return [];
  const directory = (path) => {
    try { return statSync(path).isDirectory(); } catch { return false; }
  };
  if (!hasMagic(pattern, { magicalBraces: true })) return directory(pattern) ? [pattern] : [];
  // fast-glob's terminal ** matches descendants, excluding the parent itself.
  const descendants = pattern.replace(/(^|\/)\*\*\/?$/, '$1**/*');
  return globDirectories(descendants, { follow: true, dot: false, absolute: isAbsolute(pattern) }).filter(directory);
}
