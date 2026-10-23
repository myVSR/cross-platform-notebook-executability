#!/bin/bash
#SBATCH --job-name=nbf-driver
#SBATCH --partition=standard
#SBATCH --time=3-00:00:00
#SBATCH --cpus-per-task=1
#SBATCH --mem=2G
#SBATCH --output=results/%x-%j.out
#SBATCH --signal=B:USR1@1800
# Unattended study: runs `study.py next` every NBF_DRIVER_INTERVAL seconds (default 1800) until the
# study is complete. Start it once with `bash study.sh autopilot` (from the hpc/ folder).
#   - Each round does what `bash study.sh next` would do: submit the draw / the next pass when due,
#     merge and top up after a pass. Nothing is ever submitted twice (study.py checks squeue first).
#   - Errors never stop the driver: the output of a failing round goes to results/<round>/driver-errors.log
#     and the round is tried again after the interval (e.g. sbatch refused, merge failed).
#   - Before the 3-day time limit it resubmits itself; when the study is complete it writes
#     results/<round>/COMPLETE and stops.
# Logs: results/<round>/driver.log (every round), driver-errors.log (failed rounds), results/nbf-driver-<job>.out.
#   <round> = NBF_STUDY from study.env (default study).
# Stop it: scancel --name=nbf-driver
set -u
cd "${SLURM_SUBMIT_DIR:-$(dirname "$0")}" || exit 1
[ -f "$HOME/.notebookfair/env.sh" ] && source "$HOME/.notebookfair/env.sh"
[ -f study.env ] && { set -a; source study.env; set +a; }     # the study round (NBF_STUDY, ...), see study.py
STUDY_DIR="results/${NBF_STUDY:-study}"
mkdir -p "$STUDY_DIR"
LOG="$STUDY_DIR/driver.log"
ERRORS="$STUDY_DIR/driver-errors.log"
INTERVAL="${NBF_DRIVER_INTERVAL:-1800}"
stamp() { date -Iseconds; }

resubmit() {
    echo "=== [$(stamp)] time limit near: resubmitting the driver" >> "$LOG"
    sbatch --export=ALL,NBF_DRIVER_RESUBMITS=$(( ${NBF_DRIVER_RESUBMITS:-0} + 1 )) study_driver.sbatch >> "$LOG" 2>&1 \
        || echo "=== [$(stamp)] RESUBMIT FAILED: run bash study.sh autopilot" | tee -a "$ERRORS" >> "$LOG"
    exit 0
}
trap resubmit USR1

# one driver at a time (a resubmitted driver may start while the previous one is finishing)
for attempt in 1 2 3; do
    others=$(squeue -h -u "$USER" -n nbf-driver -t RUNNING -o %i 2>/dev/null | grep -vx "${SLURM_JOB_ID:-none}" || true)
    [ -z "$others" ] && break
    [ "$attempt" = 3 ] && { echo "=== [$(stamp)] another driver ($others) is running: this one ($SLURM_JOB_ID) stops" >> "$LOG"; exit 0; }
    sleep 60
done
echo "=== [$(stamp)] driver $SLURM_JOB_ID started (resubmit ${NBF_DRIVER_RESUBMITS:-0}, every ${INTERVAL}s)" >> "$LOG"

complete() {
    python3 -c "import json, sys; sys.exit(0 if json.load(open('$STUDY_DIR/state.json')).get('complete') else 1)" 2>/dev/null
}

while true; do
    if complete; then
        echo "=== [$(stamp)] study complete: driver stops" >> "$LOG"
        python3 study.py status > "$STUDY_DIR/COMPLETE" 2>&1
        exit 0
    fi
    round_log="$STUDY_DIR/.driver-round.log"
    timeout 4h python3 study.py next > "$round_log" 2>&1 &
    wait $!                     # in the background, so the time-limit signal is handled at once
    code=$?
    { echo "=== [$(stamp)] study.py next (exit $code)"; cat "$round_log"; } >> "$LOG"
    if [ "$code" -ne 0 ]; then
        { echo "=== [$(stamp)] round failed (exit $code); retried in ${INTERVAL}s"; tail -60 "$round_log"; } >> "$ERRORS"
    fi
    sleep "$INTERVAL" &
    wait $!                     # a USR1 interrupts the wait, not only the end of a round
done
