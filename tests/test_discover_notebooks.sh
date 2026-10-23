#!/bin/bash
# Offline test: discover_notebooks registers each real notebook once and skips
# checkpoint copies, macOS resource forks, pipeline outputs and Zenodo
# duplicates listed in .zenodo-acquisition.json.
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
TEST_DIR=$(mktemp -d)
trap 'rm -rf "$TEST_DIR"' EXIT
LOG_FILE=""

source "$PROJECT_ROOT/src/logging.sh"
source "$PROJECT_ROOT/src/repo.sh"

R="$TEST_DIR/record"
mkdir -p "$R/nb/.ipynb_checkpoints" "$R/__MACOSX/nb" "$R/pkg/nb" "$R/with space"
for f in nb/a.ipynb nb/b.ipynb "with space/c.ipynb" pkg/nb/a.ipynb \
         nb/.ipynb_checkpoints/a-checkpoint.ipynb __MACOSX/nb/._a.ipynb nb/._b.ipynb; do
    echo '{}' > "$R/$f"
done
cat > "$R/.zenodo-acquisition.json" <<'JSON'
{"duplicate_notebooks": [{"path": "pkg/nb/a.ipynb", "same_as": "nb/a.ipynb"}]}
JSON

expected="nb/a.ipynb;nb/b.ipynb;with space/c.ipynb"
actual=$(discover_notebooks "$R" 2>/dev/null)
if [ "$actual" != "$expected" ]; then
    echo "[TEST] FAILED: discovered '$actual', expected '$expected'"
    exit 1
fi

rm "$R/.zenodo-acquisition.json"
actual=$(discover_notebooks "$R" 2>/dev/null)
if [ "$actual" != "nb/a.ipynb;nb/b.ipynb;pkg/nb/a.ipynb;with space/c.ipynb" ]; then
    echo "[TEST] FAILED without manifest: '$actual'"
    exit 1
fi

if [ -n "$(discover_notebooks "$TEST_DIR/empty-does-not-exist" 2>/dev/null)" ]; then
    echo "[TEST] FAILED: missing folder gave notebooks"
    exit 1
fi
echo "[TEST] Notebook discovery tests passed"
