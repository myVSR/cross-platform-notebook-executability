#!/bin/bash
# The former process_requirements (grep/awk import extraction that wrote
# $REPO_DIR/requirements.txt) is replaced by src/dependencies.py and
# src/install_env.py, called from setup_pyenv_env (src/pyenv.sh). See
# notes/chat-log.md (Step 2) for why. Kept as a no-op for scripts that still call it.
process_requirements() {
    log "[REQUIREMENT] process_requirements is replaced by src/dependencies.py (no-op)"
}
