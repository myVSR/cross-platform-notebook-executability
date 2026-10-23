@echo off
setlocal
cd /d C:\David\Work\ma-david-student
set LONG_WORKER_COUNT=1
set LONG_NOTEBOOK_TIMEOUT_SECONDS=7200
set CLASSIFICATION_ENABLED=true
set CLASSIFICATION_MODEL=gemma3:4b
set CLASSIFICATION_TIMEOUT=300
wsl.exe -e bash -lc "cd /mnt/c/David/Work/ma-david-student && env LONG_WORKER_COUNT=1 LONG_NOTEBOOK_TIMEOUT_SECONDS=7200 CLASSIFICATION_ENABLED=true CLASSIFICATION_MODEL=gemma3:4b CLASSIFICATION_TIMEOUT=300 bash pipeline/start_long_after_current_short_pass.sh"
