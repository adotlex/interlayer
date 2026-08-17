#!/usr/bin/env bash
# Re-runs ONLY the tests that failed in a previous full run.
#
#   npm run test:json      # full run, writes ./.vitest/results.json
#   npm run test:failed    # re-runs just the failures
#
# GREEN IS ONLY EVER DECLARED FROM A FULL-SUITE RUN, NEVER A FILTERED ONE.
set -uo pipefail
REPORT="${1:-./.vitest/results.json}"
OUT="${2:-./.vitest/results.json}"

COUNT=$(node scripts/failed-tests.mjs "$REPORT" --print=count)
if [ "$COUNT" -eq 0 ]; then echo "no failures in $REPORT"; exit 0; fi

mapfile -d '' ARGS < <(node scripts/failed-tests.mjs "$REPORT" --print=argv)
echo "re-running $COUNT failed test(s)"

npx vitest run "${ARGS[@]}" --reporter=dot --reporter=json --outputFile.json="$OUT"
STATUS=$?

# GUARD: a -t pattern matching nothing exits 0 with everything skipped.
node -e '
const r = require(require("path").resolve(process.argv[1]));
const ran = (r.numPassedTests ?? 0) + (r.numFailedTests ?? 0);
const want = Number(process.argv[2]);
if (ran === 0 && want > 0) {
  console.error(`FATAL: selective re-run matched 0 tests (expected ${want}). Pattern is wrong - NOT a pass.`);
  process.exit(97);
}
console.log(`selective re-run executed ${ran} test(s); ${r.numFailedTests} still failing`);
' "$OUT" "$COUNT" || exit 97

exit $STATUS
