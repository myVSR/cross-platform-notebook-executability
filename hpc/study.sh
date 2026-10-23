#!/bin/bash
# The whole study (sampling -> draw -> passes with top-ups -> study database); see study.py.
#   bash study.sh status | next [--dry-run] | pilot | classify
cd "$(dirname "${BASH_SOURCE[0]}")" || exit 1
[ -f "$HOME/.notebookfair/env.sh" ] && source "$HOME/.notebookfair/env.sh"
exec python3 study.py "$@"
