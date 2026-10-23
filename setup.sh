#!/usr/bin/env bash
###############################################################################
# CPRPMC Linux/WSL setup
#
# Run from the repository root:
#   bash setup.sh
#
# This installs the Linux system tools required by run.sh, installs pyenv when
# needed, and installs the Python packages listed in requirements.txt.
###############################################################################

set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

info() {
    echo "[SETUP] $*"
}

fail() {
    echo "[ERROR] $*" >&2
    exit 1
}

if [ "$(uname -s)" != "Linux" ]; then
    fail "This setup script must run in Linux. On Windows, use WSL: wsl bash setup.sh"
fi

if ! command -v apt-get >/dev/null 2>&1; then
    fail "This setup script currently supports Debian/Ubuntu systems with apt-get."
fi

if [ "$(id -u)" -eq 0 ]; then
    SUDO=""
else
    command -v sudo >/dev/null 2>&1 || fail "sudo is required to install system packages."
    SUDO="sudo"
fi

APT_PACKAGES=(
    git
    curl
    jq
    unzip
    libarchive-tools
    zstd
    sqlite3
    python3
    python3-pip
    python3-venv
    build-essential
    make
    libssl-dev
    zlib1g-dev
    libbz2-dev
    libreadline-dev
    libsqlite3-dev
    llvm
    libncursesw5-dev
    xz-utils
    tk-dev
    libxml2-dev
    libxmlsec1-dev
    libffi-dev
    liblzma-dev
)

info "Installing Linux system dependencies..."
$SUDO apt-get update
DEBIAN_FRONTEND=noninteractive $SUDO apt-get install -y "${APT_PACKAGES[@]}"

SYSTEM_PYTHON="${SYSTEM_PYTHON:-/usr/bin/python3}"
[ -x "$SYSTEM_PYTHON" ] || fail "System Python not found at $SYSTEM_PYTHON."
"$SYSTEM_PYTHON" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)' \
    || fail "Python 3.11 or newer is required for this project's pinned dependencies."

export PYENV_ROOT="${PYENV_ROOT:-$HOME/.pyenv}"
export PATH="$HOME/.local/bin:$PYENV_ROOT/bin:$PYENV_ROOT/shims:$PATH"

if ! command -v pyenv >/dev/null 2>&1; then
    info "Installing pyenv..."
    curl https://pyenv.run | bash
else
    info "pyenv already installed."
fi

export PATH="$HOME/.local/bin:$PYENV_ROOT/bin:$PYENV_ROOT/shims:$PATH"
if command -v pyenv >/dev/null 2>&1; then
    eval "$(pyenv init - bash)"
fi

if ! grep -q 'CPRPMC setup' "$HOME/.bashrc" 2>/dev/null; then
    info "Adding pyenv and user Python paths to ~/.bashrc..."
    {
        echo ""
        echo "# CPRPMC setup"
        echo 'export PYENV_ROOT="$HOME/.pyenv"'
        echo '[[ -d "$PYENV_ROOT/bin" ]] && export PATH="$PYENV_ROOT/bin:$PATH"'
        echo 'export PATH="$HOME/.local/bin:$PYENV_ROOT/shims:$PATH"'
        echo 'command -v pyenv >/dev/null 2>&1 && eval "$(pyenv init - bash)"'
    } >> "$HOME/.bashrc"
fi

PIP_FLAGS=(--user)
if "$SYSTEM_PYTHON" -m pip install --help 2>/dev/null | grep -q -- '--break-system-packages'; then
    PIP_FLAGS+=(--break-system-packages)
fi

info "Installing Python dependencies from requirements.txt..."
"$SYSTEM_PYTHON" -m pip install "${PIP_FLAGS[@]}" --upgrade pip
"$SYSTEM_PYTHON" -m pip install "${PIP_FLAGS[@]}" -r "$PROJECT_ROOT/requirements.txt"

info "Installing Knowledge Graph dependencies..."
KG_VENV="$PROJECT_ROOT/kg/graph/.venv"
"$SYSTEM_PYTHON" -m venv "$KG_VENV"
"$KG_VENV/bin/python" -m pip install --upgrade pip
"$KG_VENV/bin/python" -m pip install -r "$PROJECT_ROOT/kg/graph/requirements-kg.txt"

# Licence detection (src/licence.py) runs licensee in Docker. Optional: without
# it, runs record the licence status "unavailable".
if command -v docker >/dev/null 2>&1; then
    info "Building the licensee image (notebookfair/licensee:10.1.0)..."
    docker build -q -t notebookfair/licensee:10.1.0 "$PROJECT_ROOT/docker/licensee" ||
        info "Could not build the licensee image; licence detection will be unavailable."
else
    info "Docker not found; licence detection (licensee) will be unavailable."
fi

info "Verifying required commands..."
for command_name in python3 sqlite3 jq unzip bsdtar git pyenv jupyter; do
    command -v "$command_name" >/dev/null 2>&1 || fail "$command_name is still not available on PATH."
done

info "Setup complete. You can now run: bash run.sh"
