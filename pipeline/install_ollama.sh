#!/bin/bash
###############################################################################
# install_ollama.sh — Ollama in user space (no root) for the classification stage on an HPC
#
# Installs the pinned Ollama release into $NBF_HOME/ollama (binary + its CUDA libraries), keeps models
# in $NBF_HOME/ollama/models, pulls the classification model and records versions and the model digest
# in $NBF_HOME/ollama/install.json. Safe to run again (skips what is already there).
#   bash pipeline/install_ollama.sh            # login node (needs internet; ~1.4 GB + ~3.3 GB model)
# Settings: OLLAMA_VERSION (default v0.32.15, the workstation's version), CLASSIFICATION_MODEL
#           (default gemma3:4b), NBF_HOME (default ~/.notebookfair).
# Then: sbatch hpc/classify.sbatch results/<run>
###############################################################################
set -euo pipefail
NBF_HOME="${NBF_HOME:-$HOME/.notebookfair}"
VERSION="${OLLAMA_VERSION:-v0.32.15}"
MODEL="${CLASSIFICATION_MODEL:-gemma3:4b}"
DIR="$NBF_HOME/ollama"
export OLLAMA_MODELS="$DIR/models"
mkdir -p "$DIR" "$OLLAMA_MODELS"

if [ -x "$DIR/bin/ollama" ] && "$DIR/bin/ollama" --version 2>/dev/null | grep -q "${VERSION#v}"; then
    echo "[OLLAMA] $VERSION already installed in $DIR"
else
    archive="$DIR/ollama-linux-amd64-$VERSION.tar.zst"
    url="https://github.com/ollama/ollama/releases/download/$VERSION/ollama-linux-amd64.tar.zst"
    [ -f "$archive" ] || curl -fL --retry 3 -o "$archive" "$url"
    echo "[OLLAMA] archive sha256 $(sha256sum "$archive" | cut -d' ' -f1)"
    rm -rf "$DIR/bin" "$DIR/lib"
    if command -v zstd >/dev/null 2>&1; then
        tar --use-compress-program=zstd -xf "$archive" -C "$DIR"
    else                                   # no zstd on the system: decompress with Python
        python3 -c 'import zstandard' 2>/dev/null || python3 -m pip install -q zstandard
        python3 - "$archive" "$DIR" <<'PY'
import sys, tarfile, zstandard
with open(sys.argv[1], 'rb') as raw, zstandard.ZstdDecompressor().stream_reader(raw) as stream:
    with tarfile.open(fileobj=stream, mode='r|') as archive:
        archive.extractall(sys.argv[2])
PY
    fi
    rm -f "$archive"
fi
"$DIR/bin/ollama" --version

# pull the model with a temporary server on a free local port
port=$(( 20000 + RANDOM % 20000 ))
export OLLAMA_HOST="127.0.0.1:$port"
"$DIR/bin/ollama" serve > "$DIR/serve-install.log" 2>&1 &
server=$!
trap 'kill "$server" 2>/dev/null || true' EXIT
for _ in $(seq 60); do curl -sf "http://$OLLAMA_HOST/api/version" >/dev/null && break; sleep 2; done
"$DIR/bin/ollama" pull "$MODEL"
python3 - "$OLLAMA_HOST" "$MODEL" "$VERSION" "$DIR/install.json" <<'PY'
import json, sys, urllib.request
host, model, version, out = sys.argv[1:5]
tags = json.load(urllib.request.urlopen(f'http://{host}/api/tags'))['models']
digest = next((m['digest'] for m in tags if m['name'] in (model, f'{model}:latest')), None)
server = json.load(urllib.request.urlopen(f'http://{host}/api/version'))['version']
json.dump(dict(ollama_release=version, ollama_version=server, model=model, model_digest=digest), open(out, 'w'), indent=2)
print(f'[OLLAMA] {model} digest {digest}, server {server} -> {out}')
PY
