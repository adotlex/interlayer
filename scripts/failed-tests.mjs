#!/usr/bin/env node
// Reads a Vitest JSON report and prints argv for a selective re-run.
// Usage: node scripts/failed-tests.mjs <report.json> [--print=argv|json|count]
import { readFileSync } from 'node:fs';
import { relative } from 'node:path';

const escapeRe = (s) => s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');

const reportPath = process.argv[2] ?? './.vitest/results.json';
const mode = (process.argv[3] ?? '--print=argv').split('=')[1];
const report = JSON.parse(readFileSync(reportPath, 'utf8'));

const failures = [];
for (const suite of report.testResults ?? []) {
  // A suite can fail to even load (import/syntax error) -> no assertions.
  if (suite.status === 'failed' && (suite.assertionResults ?? []).length === 0) {
    failures.push({ file: suite.name, fullName: null, loadError: true });
    continue;
  }
  for (const a of suite.assertionResults ?? []) {
    if (a.status === 'failed') {
      failures.push({
        file: suite.name,
        fullName: a.fullName,
        line: a.location?.line ?? null,
        messages: a.failureMessages ?? [],
      });
    }
  }
}

if (mode === 'json') {
  console.log(JSON.stringify(failures, null, 2));
  process.exit(0);
}
if (mode === 'count') {
  console.log(String(failures.length));
  process.exit(0);
}
if (failures.length === 0) process.exit(0);

const files = [...new Set(failures.map((f) => relative(process.cwd(), f.file)))];
const named = failures.filter((f) => f.fullName);
const args = [...files];
// If any file failed to load, re-run those files wholesale (no -t).
if (named.length > 0 && !failures.some((f) => f.loadError)) {
  args.push('-t', `^(${named.map((f) => escapeRe(f.fullName)).join('|')})$`);
}
// NUL-separated so names containing spaces/pipes survive the shell handoff.
process.stdout.write(args.join('\0'));
