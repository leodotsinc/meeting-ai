'use strict';
// Same range semantics as npm/arborist dep-valid.js: satisfies(..., true).
// The tool is installed from a separate reviewed lock, never candidate packages.
const fs = require('node:fs');
const path = require('node:path');
const root = path.join(__dirname, 'maintenance-tools/node_modules/semver');
const semver = require(root);
if (require(path.join(root, 'package.json')).version !== '7.8.5') process.exit(2);
let size = 0; const chunks = [];
process.stdin.on('data', chunk => {
  size += chunk.length;
  if (size > 4 * 1024 * 1024) process.exit(2);
  chunks.push(chunk);
});
process.stdin.on('end', () => {
  try {
    const rows = JSON.parse(Buffer.concat(chunks));
    if (!Array.isArray(rows) || rows.length > 20000) process.exit(2);
    const values = rows.map(row => {
      if (!Array.isArray(row) || row.length !== 2 || row.some(v => typeof v !== 'string' || v.length > 1024)) throw Error();
      return semver.valid(row[0]) !== null && semver.satisfies(row[0], row[1], true);
    });
    process.stdout.write(JSON.stringify(values));
  } catch { process.exit(2); }
});
