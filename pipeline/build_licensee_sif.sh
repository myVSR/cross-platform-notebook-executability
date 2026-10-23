#!/bin/bash
###############################################################################
# build_licensee_sif.sh — the licensee image (docker/licensee) for machines without Docker
#
# Licence detection (src/licence.py) runs licensee in the Docker image notebookfair/licensee:10.1.0.
# the cluster has no Docker but Singularity: the *same* image is exported on the workstation and converted
# to a SIF there (no root needed for a docker-archive), so both runtimes use identical files.
#
#   1. workstation:  bash pipeline/build_licensee_sif.sh export
#                    -> licensee-10.1.0.docker.tar + .json (image id); copy both to the cluster, e.g.
#                       scp licensee-10.1.0.docker.tar* hpc:~/.notebookfair/
#   2. the cluster:        bash pipeline/build_licensee_sif.sh build [~/.notebookfair/licensee-10.1.0.docker.tar]
#                    -> ~/.notebookfair/licensee-10.1.0.sif (+ .json) and a test run
# Settings: LICENSEE_IMAGE (default notebookfair/licensee:10.1.0), LICENSEE_SIF
#           (default ~/.notebookfair/licensee-10.1.0.sif), OUT_DIR for export (default .).
###############################################################################
set -euo pipefail
IMAGE="${LICENSEE_IMAGE:-notebookfair/licensee:10.1.0}"
SIF="${LICENSEE_SIF:-$HOME/.notebookfair/licensee-10.1.0.sif}"
PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

case "${1:-}" in
    export)
        out="${OUT_DIR:-.}/licensee-10.1.0.docker.tar"
        docker image inspect "$IMAGE" >/dev/null 2>&1 ||
            docker build -t "$IMAGE" "$PROJECT_ROOT/docker/licensee"
        docker save "$IMAGE" -o "$out"
        id=$(docker image inspect "$IMAGE" --format '{{.Id}}')
        printf '{"image": "%s", "docker_image_id": "%s", "exported": "%s", "tar_sha256": "%s"}\n' \
            "$IMAGE" "$id" "$(date -Iseconds)" "$(sha256sum "$out" | cut -d' ' -f1)" > "$out.json"
        echo "[LICENSEE] exported $IMAGE ($id) -> $out ($(du -h "$out" | cut -f1)); copy $out and $out.json to the HPC"
        ;;
    build)
        tar="${2:-$HOME/.notebookfair/licensee-10.1.0.docker.tar}"
        [ -f "$tar" ] || { echo "[LICENSEE] $tar not found (run 'export' on the workstation and copy it)"; exit 1; }
        binary=$(command -v apptainer || command -v singularity) ||
            { echo "[LICENSEE] neither apptainer nor singularity found"; exit 1; }
        if [ -f "$tar.json" ]; then
            expected=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["tar_sha256"])' "$tar.json")
            [ "$(sha256sum "$tar" | cut -d' ' -f1)" = "$expected" ] || { echo "[LICENSEE] checksum mismatch: $tar"; exit 1; }
        fi
        mkdir -p "$(dirname "$SIF")"
        "$binary" build --force "$SIF" "docker-archive://$tar"
        [ -f "$tar.json" ] && cp "$tar.json" "$SIF.json"
        mkdir -p "$SIF.test" && printf 'MIT License\n\nCopyright (c) 2026 x\n\nPermission is hereby granted, free of charge, to any person obtaining a copy of this software and associated documentation files (the "Software"), to deal in the Software without restriction, including without limitation the rights to use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies of the Software, and to permit persons to whom the Software is furnished to do so, subject to the following conditions:\n\nThe above copyright notice and this permission notice shall be included in all copies or substantial portions of the Software.\n\nTHE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.\n' > "$SIF.test/LICENSE"
        result=$(LICENSEE_RUNTIME=singularity LICENSEE_SIF="$SIF" python3 "$PROJECT_ROOT/src/licence.py" "$SIF.test" --json || true)
        rm -rf "$SIF.test"
        echo "[LICENSEE] test: $result"
        grep -q '"licence_file": "MIT"' <<<"$result" && echo "[LICENSEE] $SIF works" ||
            { echo "[LICENSEE] test failed"; exit 1; }
        ;;
    *)
        sed -n '2,17p' "$0"; exit 2 ;;
esac
