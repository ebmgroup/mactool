#!/bin/bash
# Run the statistics-upload test suite.
#
#   ./tests/run_all.sh
#
# Each test gets a freshly built fixture directory, because config.json is
# rewritten by config.save() during the API test and would otherwise leak into
# the next run. Nothing here touches a real Supabase — every target is a local
# mock — so the suite is safe to run anywhere.

set -u

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORKDIR="${TMPDIR:-/tmp}/mactool-tests-$$"
PYTHON="${PYTHON:-python3}"

TESTS=(
  test_parity.py       # rows must match the legacy upload script byte for byte
  test_flow.py         # dry run, ALTER-SQL, LaunchAgent detection
  test_upload.py       # batching, dedupe, retry, column filtering
  test_api.py          # routes, key masking, config, dashboard stats toggle
  test_columns.py      # unreliable schema discovery must not drop fields
  test_incomplete.py   # a target without a key is not "success"
  test_permission.py   # missing write rights abort at once
  test_schema.py       # Accept-Profile / Content-Profile must be sent
  test_integrity.py    # kaputte super.db: kein Upload, dafuer eine Mail
  test_remote.py       # Fernzugriff: Reservieren, Verfall, Maskierung, Diagnose
  test_warnings.py     # Warn-ZIPs des Bots: Suche, Namenspruefung, Bildschirmtexte, Stuecke
)

mkdir -p "$WORKDIR"
trap 'rm -rf "$WORKDIR"' EXIT
cd "$WORKDIR" || exit 1

failed=0
for test in "${TESTS[@]}"; do
  "$PYTHON" "$REPO/tests/make_fixtures.py" >/dev/null || exit 1
  printf '%-22s ' "$test"
  if output=$("$PYTHON" "$REPO/tests/$test" 2>&1); then
    echo "OK"
  else
    echo "FEHLGESCHLAGEN"
    echo "$output" | grep -E "FAIL|Error|Traceback" | head -5 | sed 's/^/    /'
    failed=1
  fi
done

rm -f "$REPO/run_state.json"

if [ "$failed" -eq 0 ]; then
  echo "Alle Suites bestanden."
else
  echo "Mindestens eine Suite ist fehlgeschlagen."
fi
exit "$failed"
