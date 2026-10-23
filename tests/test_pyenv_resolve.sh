#!/bin/bash
# Offline test: ensure_pyenv_version resolves "3.8" to the newest 3.8.x and an
# exact "3.8.10" to itself (it used to fail), using a stand-in for pyenv.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
TEST_DIR=$(mktemp -d)
trap 'rm -rf "$TEST_DIR"' EXIT
LOG_FILE="$TEST_DIR/log"
export PYENV_ROOT="$TEST_DIR/pyenv"

# a stand-in interpreter at $PYENV_ROOT/versions/<v>/bin/python
fake_python() { mkdir -p "$PYENV_ROOT/versions/$1/bin"; ln -sf "$(command -v python3)" "$PYENV_ROOT/versions/$1/bin/python"; }
fake_python 3.11.9
fake_python 3.9.20
LISTED="3.11.9"              # `pyenv versions` misses 3.9.20, as on the cluster (run-9150494)
pyenv() {
    case "$1 $2" in
        "init -") ;;
        "versions --bare") printf '%s\n' $LISTED ;;
        "install --list") printf '  %s\n' 2.7.18 3.8.9 3.8.10 3.8.20 3.9.20 3.10.14 3.10.15 3.13.0rc1 3.13.1 ;;
        "install 3.10.14") fake_python 3.10.14; echo "pyenv: $PYENV_ROOT/versions/3.10.14 already exists"; return 1 ;;
        install*) fake_python "$2"; echo "installed $2" ;;
    esac
}
source "$PROJECT_ROOT/src/logging.sh"
source "$PROJECT_ROOT/src/pyenv.sh"

failures=0
expect() {
    local requested="$1" expected="$2" actual
    actual=$(ensure_pyenv_version "$requested" 2>/dev/null)
    if [ "$actual" = "$expected" ]; then echo "[TEST] ok: $requested -> $actual"
    else echo "[TEST] FAILED: $requested -> '$actual', expected '$expected'"; failures=$((failures + 1)); fi
}
expect 3.8 3.8.20          # newest patch (numeric, not lexical: 20 > 9)
expect 3.8.10 3.8.10       # exact patch kept
expect 3.8.11 3.8.20       # unknown patch -> newest of that minor
expect 3.10 3.10.15
expect 3.11.9 3.11.9       # already installed
expect 3.13 3.13.1         # release candidates ignored
expect 3.5 ""              # nothing installable
expect 3.9 3.9.20          # installed but not listed by `pyenv versions`: no reinstall
expect 3.10.14 3.10.14     # install says "already exists" but the interpreter is there
[ "$failures" -eq 0 ] && echo "[TEST] pyenv resolution tests passed" || exit 1
