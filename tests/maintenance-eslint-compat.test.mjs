import assert from 'node:assert/strict';
import test from 'node:test';
import { fileURLToPath } from 'node:url';
import { ESLint } from 'eslint';

// Exercise the real, committed configuration and installed plugins. No rule
// overrides, injected plugin mocks or suppression of the existing Hooks errors.
const cwd = fileURLToPath(new URL('../', import.meta.url));
const eslint = new ESLint({ cwd });
const filePath = 'src/maintenance-compat.synthetic.tsx';
const cases = [
  {
    rule: 'import/no-anonymous-default-export',
    invalid: 'export default { synthetic: true };',
    valid: 'const sample = { synthetic: true }; export default sample;',
  },
  {
    rule: 'jsx-a11y/alt-text',
    invalid: 'export function Sample() { return <img src="/synthetic.png" />; }',
    valid: 'export function Sample() { return <img src="/synthetic.png" alt="Synthetic fixture" />; }',
  },
  {
    rule: 'react/jsx-key',
    invalid: 'export function Sample() { return [<span>synthetic</span>]; }',
    valid: 'export function Sample() { return [<span key="synthetic">synthetic</span>]; }',
  },
];

for (const { rule, invalid, valid } of cases) {
  test(`real compatibility config detects and clears ${rule}`, async () => {
    const config = await eslint.calculateConfigForFile(filePath);
    assert.ok(config, 'the synthetic TSX file must be included by the real config');
    assert.ok(config.rules[rule]?.[0] > 0, `${rule} must remain enabled`);
    const [bad] = await eslint.lintText(invalid, { filePath });
    const [good] = await eslint.lintText(valid, { filePath });
    for (const result of [bad, good]) {
      assert.equal(result.fatalErrorCount, 0, JSON.stringify(result.messages));
      assert.ok(result.messages.every(message => message.ruleId), 'no ignored-file/parser diagnostic');
    }
    assert.equal(bad.messages.filter(message => message.ruleId === rule).length, 1);
    assert.equal(good.messages.filter(message => message.ruleId === rule).length, 0);
    // Next's separate no-img-element warning is intentionally not suppressed.
    assert.equal(good.errorCount, 0, JSON.stringify(good.messages));
  });
}
