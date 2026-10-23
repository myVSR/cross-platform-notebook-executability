@echo off
setlocal
cd /d C:\David\Work\ma-david-student
if not exist data/output\launcher mkdir data/output\launcher
for /f %%i in ('powershell -NoProfile -Command "Get-Date -Format yyyyMMdd-HHmmss"') do set RUN_STAMP=%%i
set RUN_DIR_WIN=data/output\launcher\manual-worker0-%RUN_STAMP%
set RUN_DIR_WSL=data/output/launcher/manual-worker0-%RUN_STAMP%
mkdir "%RUN_DIR_WIN%"
wsl.exe -e bash -lc "cd /mnt/c/David/Work/ma-david-student && env WORKER_COUNT=2 WORKER_INDEX=0 LIMIT=0 CLASSIFICATION_ENABLED=false bash pipeline/run_full_sample.sh > '%RUN_DIR_WSL%/worker0.out' 2>&1"
