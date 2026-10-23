#!/bin/bash
###############################################################################
# setup_full_run_env.sh
#
# Best-effort, idempotent dependency setup for running the full 252-entry
# evaluation sample through run.sh (batch of GitHub + Codeberg + Zenodo repos).
#
# Put this file in the pipeline/ directory of the repo (next to run.sh) and
# run it once before run_full_sample.sh:
#
#   bash pipeline/setup_full_run_env.sh
#
# It tries a normal `sudo apt-get install` first. If that's not available (no
# root — common on managed/sandboxed Linux environments), it falls back to
# `apt-get download` + manual extraction into ~/localpkgs and ~/bin, which
# does NOT need root, and writes ~/pipeline_env.sh with the PATH/env changes
# that fallback needs. If that file gets created, source it in every new
# shell before running the pipeline:
#
#   source ~/pipeline_env.sh
#
# Safe to re-run — it skips anything already present.
###############################################################################
set -uo pipefail

echo "============================================"
echo " Full-sample run — environment setup"
echo "============================================"

if ! command -v apt-get >/dev/null 2>&1; then
    echo ""
    echo "[WARN] apt-get not found — this script's auto-install logic targets"
    echo "       Debian/Ubuntu (which is what run.sh itself assumes). Install"
    echo "       sqlite3, pyenv, jupyter/nbconvert and nbdime for your OS's"
    echo "       package manager instead, then re-run this script — it will"
    echo "       detect what's already there and skip it."
fi

HAVE_SUDO=false
if command -v sudo >/dev/null 2>&1 && sudo -n true 2>/dev/null; then
    HAVE_SUDO=true
elif [ "$(id -u)" = "0" ]; then
    HAVE_SUDO=true
fi

NEED_ENV_FILE=false
ENV_FILE="$HOME/pipeline_env.sh"
mkdir -p "$HOME/bin"

apt_install() {
    # $* = package name(s), space separated. Returns 1 if we have no way to
    # install system-wide (no sudo, not root) so the caller can fall back.
    command -v apt-get >/dev/null 2>&1 || return 1
    if [ "$(id -u)" = "0" ]; then
        apt-get update -qq && apt-get install -y "$@"
    elif $HAVE_SUDO; then
        sudo apt-get update -qq && sudo apt-get install -y "$@"
    else
        return 1
    fi
}

# Downloading a .deb does NOT need root, only installing it system-wide does.
# This fetches the package and unpacks it into a local prefix instead.
apt_download_extract() {
    # $1 = package name
    local pkg="$1"
    command -v apt-get >/dev/null 2>&1 || return 1
    local tmpdir
    tmpdir=$(mktemp -d)
    ( cd "$tmpdir" && apt-get download "$pkg" >/dev/null 2>&1 ) || { rm -rf "$tmpdir"; return 1; }
    local deb
    deb=$(ls "$tmpdir"/*.deb 2>/dev/null | head -1)
    [ -n "$deb" ] || { rm -rf "$tmpdir"; return 1; }
    mkdir -p "$HOME/localpkgs"
    dpkg-deb -x "$deb" "$HOME/localpkgs"
    rm -rf "$tmpdir"
    NEED_ENV_FILE=true
    return 0
}

echo ""
echo "[1/7] sqlite3 (the CLI binary, not just Python's sqlite3 module — run.sh"
echo "      and every DB query in the pipeline shell out to it directly)"
if command -v sqlite3 >/dev/null 2>&1; then
    echo "      already present: $(sqlite3 --version)"
else
    if apt_install sqlite3; then
        echo "      installed via apt"
    else
        echo "      no root — downloading the .deb directly and extracting it locally"
        if apt_download_extract sqlite3 && [ -f "$HOME/localpkgs/usr/bin/sqlite3" ]; then
            cp "$HOME/localpkgs/usr/bin/sqlite3" "$HOME/bin/sqlite3"
            chmod +x "$HOME/bin/sqlite3"
            echo "      extracted to ~/bin/sqlite3"
        else
            echo "      [WARN] could not obtain sqlite3 — run.sh will refuse to start until this is fixed."
        fi
    fi
fi

echo ""
echo "[2/7] pyenv"
if command -v pyenv >/dev/null 2>&1 || [ -d "$HOME/.pyenv" ]; then
    echo "      already present"
else
    git clone --depth 1 https://github.com/pyenv/pyenv.git "$HOME/.pyenv"
    NEED_ENV_FILE=true
fi
export PYENV_ROOT="$HOME/.pyenv"
export PATH="$PYENV_ROOT/bin:$PYENV_ROOT/shims:$HOME/bin:$PATH"
eval "$(pyenv init -)" 2>/dev/null || true

echo ""
echo "[3/7] Python build headers (needed by pyenv to compile a Python version"
echo "      from source: openssl, sqlite3, readline, bz2, lzma, ncurses)"
MISSING_DEV=()
for hdr_pkg in libssl-dev libbz2-dev libreadline-dev libsqlite3-dev liblzma-dev libncurses-dev zlib1g-dev libffi-dev; do
    dpkg -s "$hdr_pkg" >/dev/null 2>&1 || MISSING_DEV+=("$hdr_pkg")
done
if [ ${#MISSING_DEV[@]} -eq 0 ]; then
    echo "      already present"
elif apt_install "${MISSING_DEV[@]}"; then
    echo "      installed via apt"
else
    echo "      no root — downloading + extracting headers locally"
    for pkg in "${MISSING_DEV[@]}"; do
        apt_download_extract "$pkg" && echo "      extracted $pkg" || echo "      [WARN] could not obtain $pkg"
    done
    export CPPFLAGS="-I$HOME/localpkgs/usr/include ${CPPFLAGS:-}"
    export LDFLAGS="-L$HOME/localpkgs/usr/lib/x86_64-linux-gnu ${LDFLAGS:-}"
    export LD_LIBRARY_PATH="$HOME/localpkgs/usr/lib/x86_64-linux-gnu:${LD_LIBRARY_PATH:-}"
    # Some -dev packages only ship the versioned .so (e.g. libssl.so.3); the
    # linker needs the unversioned name too, and the real runtime lib is
    # already on the system (it's what everything else already links against),
    # so just point the unversioned name at it.
    LIBDIR="$HOME/localpkgs/usr/lib/x86_64-linux-gnu"
    if [ -d "$LIBDIR" ]; then
        for base in libssl libcrypto libsqlite3 libbz2 libreadline liblzma; do
            target=$(ls /lib/x86_64-linux-gnu/${base}.so.* /usr/lib/x86_64-linux-gnu/${base}.so.* 2>/dev/null | sort -V | tail -1)
            [ -n "$target" ] && ln -sf "$target" "$LIBDIR/${base}.so"
        done
    fi
fi

echo ""
echo "[4/7] A usable Python 3.10 for pyenv (repos with no version hint of"
echo "      their own default to 3.10 — see src/pyenv.sh:detect_python_version)"
RESOLVED_310="$(pyenv versions --bare 2>/dev/null | grep -E '^3\.10\.' | tail -1)"
if [ -n "$RESOLVED_310" ]; then
    echo "      a 3.10.x pyenv version is already installed: $RESOLVED_310"
else
    RESOLVED_310=$(pyenv install --list 2>/dev/null | grep -E '^\s+3\.10\.[0-9]+$' | grep -vE '(dev|a|b|rc)' | tail -1 | xargs)
    echo "      trying a real build of Python $RESOLVED_310 via pyenv (can take a few minutes)..."
    if pyenv install -s "$RESOLVED_310" >/tmp/pyenv_install_$$.log 2>&1; then
        echo "      built Python $RESOLVED_310"
    else
        tail -5 "/tmp/pyenv_install_$$.log"
        echo "      [WARN] could not build Python from source (often means python.org"
        echo "             isn't reachable from this network)."
        SYS_PY310=$(command -v python3.10 || true)
        if [ -n "$SYS_PY310" ]; then
            echo "      falling back: aliasing pyenv's \"$RESOLVED_310\" to the system's"
            echo "      existing $("$SYS_PY310" --version) — that repo won't get true"
            echo "      version isolation, just whatever the system interpreter is."
            mkdir -p "$PYENV_ROOT/versions/$RESOLVED_310/bin"
            for exe in python python3 python3.10; do
                ln -sf "$SYS_PY310" "$PYENV_ROOT/versions/$RESOLVED_310/bin/$exe"
            done
        else
            echo "      [WARN] no system python3.10 found either — repos defaulting to"
            echo "             3.10 will fail to get an environment until this is fixed."
            RESOLVED_310=""
        fi
    fi
    rm -f "/tmp/pyenv_install_$$.log"
fi
# pyenv intercepts `python3`/`pip3` via shims and resolves them against
# whatever version is currently active — if that's "system" (or anything
# without pip installed in it), `pip3` fails with "command not found" even
# though a perfectly good interpreter with pip exists under a pyenv version.
# Point pyenv's global default at the 3.10.x we just confirmed works, so the
# rest of this script (and run.sh's own `jupyter` check) resolve correctly.
if [ -n "$RESOLVED_310" ]; then
    CURRENT_GLOBAL=$(pyenv global 2>/dev/null || echo "")
    if [ "$CURRENT_GLOBAL" != "$RESOLVED_310" ]; then
        pyenv global "$RESOLVED_310"
        echo "      set pyenv global -> $RESOLVED_310 (was: ${CURRENT_GLOBAL:-unset})"
    fi
    pyenv rehash
fi

echo ""
echo "[5/7] jq, unzip, git (run.sh's own dependency check requires all three;"
echo "      unzip and git are almost always already present)"
MISSING_TOOLS=()
for tool in jq unzip git; do
    command -v "$tool" >/dev/null 2>&1 || MISSING_TOOLS+=("$tool")
done
if [ ${#MISSING_TOOLS[@]} -eq 0 ]; then
    echo "      already present"
elif apt_install "${MISSING_TOOLS[@]}"; then
    echo "      installed via apt"
else
    echo "      no root — downloading + extracting locally"
    for pkg in "${MISSING_TOOLS[@]}"; do
        if apt_download_extract "$pkg" && [ -f "$HOME/localpkgs/usr/bin/$pkg" ]; then
            cp "$HOME/localpkgs/usr/bin/$pkg" "$HOME/bin/$pkg"
            chmod +x "$HOME/bin/$pkg"
            echo "      extracted $pkg to ~/bin/$pkg"
            if [ "$pkg" = "jq" ]; then
                # jq's binary package depends on shared libraries that are not
                # included in the jq .deb itself.
                apt_download_extract libjq1 && echo "      extracted libjq1"
                apt_download_extract libonig5 && echo "      extracted libonig5"
            fi
        else
            echo "      [WARN] could not obtain $pkg this way — it may depend on"
            echo "             shared libraries not covered here; install it manually."
        fi
    done
fi

echo ""
echo "[6/7] jupyter + nbconvert on PATH (run.sh's own dependency check requires this)"
if command -v jupyter >/dev/null 2>&1; then
    echo "      already present"
else
    if ! pip3 install --user --quiet jupyter nbconvert; then
        echo "      [WARN] pip3 install failed — see the pyenv global line above;"
        echo "             if it still fails, try: python3 -m pip install --user jupyter nbconvert"
    fi
    export PATH="$HOME/.local/bin:$PATH"
    pyenv rehash 2>/dev/null || true
    NEED_ENV_FILE=true
fi

echo ""
echo "[7/7] nbdime (the pipeline's own analysis/compare_notebook.py needs this —"
echo "      without it, every notebook comparison silently crashes and no"
echo "      reproducibility score gets recorded at all)"
if python3 -c "import nbdime" >/dev/null 2>&1; then
    echo "      already present"
else
    pip3 install --user --quiet nbdime || python3 -m pip install --user --quiet nbdime
fi

if $NEED_ENV_FILE || [ -f "$ENV_FILE" ]; then
    cat > "$ENV_FILE" << 'EOF'
export PATH="$HOME/bin:$HOME/.pyenv/bin:$HOME/.pyenv/shims:$HOME/.local/bin:$PATH"
export PYENV_ROOT="$HOME/.pyenv"
export CPPFLAGS="-I$HOME/localpkgs/usr/include"
export LDFLAGS="-L$HOME/localpkgs/usr/lib/x86_64-linux-gnu"
export LD_LIBRARY_PATH="$HOME/localpkgs/usr/lib/x86_64-linux-gnu:${LD_LIBRARY_PATH:-}"
if command -v pyenv >/dev/null 2>&1; then eval "$(pyenv init -)"; fi
EOF
    echo ""
    echo "[SETUP] Wrote $ENV_FILE — source it in every new shell before running the pipeline:"
    echo "        source $ENV_FILE"
fi

echo ""
echo "============================================"
echo " Verifying run.sh's own dependency checks:"
echo "============================================"
# shellcheck disable=SC1090
source "$ENV_FILE" 2>/dev/null || true
ALL_OK=true
for cmd in python3 sqlite3 jq unzip git pyenv jupyter; do
    if command -v "$cmd" >/dev/null 2>&1; then
        echo "  [OK]      $cmd"
    else
        echo "  [MISSING] $cmd"
        ALL_OK=false
    fi
done
echo ""
if $ALL_OK; then
    echo "All good — you can now run: bash pipeline/run_full_sample.sh"
else
    echo "Some dependencies are still missing (see [MISSING] above) — jq/unzip/git"
    echo "are simple to add by hand (apt-get install jq unzip git or your OS's"
    echo "equivalent); re-run this script afterwards to confirm."
fi
