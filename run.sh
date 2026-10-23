#!/bin/bash
###############################################################################
# CPRPMC — Entry Point
#
# Usage: ./run.sh
#
# This script checks dependencies, then launches the pipeline.
###############################################################################

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYENV_ROOT="${PYENV_ROOT:-$HOME/.pyenv}"
export PYENV_ROOT
export PATH="$HOME/.local/bin:$PYENV_ROOT/bin:$PYENV_ROOT/shims:$PATH"

echo "============================================"
echo "   CPRPMC Reproducibility Pipeline"
echo "============================================"

fail() { echo "[ERROR] $*"; exit 1; }

command -v python3 >/dev/null 2>&1 || fail "python3 not found. Please install Python 3."
command -v sqlite3 >/dev/null 2>&1 || fail "sqlite3 not found. Please install SQLite."
command -v jq      >/dev/null 2>&1 || fail "jq not found. Please install jq."
command -v unzip   >/dev/null 2>&1 || fail "unzip not found. Please install unzip."
command -v curl    >/dev/null 2>&1 || fail "curl not found. Please install curl."
command -v git     >/dev/null 2>&1 || fail "git not found. Please install git."
command -v pyenv   >/dev/null 2>&1 || fail "pyenv not found. See https://github.com/pyenv/pyenv#installation"
command -v jupyter >/dev/null 2>&1 || fail "jupyter not found. Run: pip install jupyter nbconvert"

echo "[CHECK] All dependencies found."
echo ""

exec bash "$SCRIPT_DIR/pipeline/main.sh" "$@"
