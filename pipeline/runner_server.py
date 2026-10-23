#!/usr/bin/env python3
"""Loopback-only job service that runs the Bash pipeline inside Docker."""

from __future__ import annotations

import argparse
import json
import os
import platform
import re
import shlex
import sqlite3
import subprocess
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path, PurePosixPath
from urllib.parse import parse_qs, unquote, urlparse


ROOT = Path(__file__).resolve().parents[1]
DATA_OUTPUT_ROOT = ROOT / "data" / "output"
MAIN_DB_DIR = DATA_OUTPUT_ROOT / "db"
JOBS_ROOT = DATA_OUTPUT_ROOT / "web-jobs"
IMAGE_NAME = os.environ.get("NOTEBOOKFAIR_PIPELINE_IMAGE", "notebookfair-pipeline:local")
MAX_BODY_BYTES = 256 * 1024
MAX_UPLOAD_BYTES = 25 * 1024 * 1024
MAX_NOTEBOOKS = 2000
MAX_LOG_LINES = 80
JOB_TIMEOUT_SECONDS = int(os.environ.get("NOTEBOOKFAIR_JOB_TIMEOUT", "7200"))
SUPPORTED_HOSTS = {"github.com", "codeberg.org", "zenodo.org", "www.zenodo.org"}
TERMINAL_STATES = {"succeeded", "partial", "failed", "cancelled"}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def atomic_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    temporary.replace(path)


def validate_repository_url(value: object) -> str:
    if not isinstance(value, str) or len(value) > 2048:
        raise ValueError("A valid repository URL is required")
    parsed = urlparse(value.strip())
    if parsed.scheme != "https" or parsed.hostname not in SUPPORTED_HOSTS:
        raise ValueError("Only HTTPS GitHub, Codeberg, and Zenodo URLs are supported")
    if parsed.username or parsed.password or parsed.port:
        raise ValueError("Repository credentials and custom ports are not accepted")
    decoded_path = unquote(parsed.path)
    if ".." in PurePosixPath(decoded_path).parts or "\\" in decoded_path:
        raise ValueError("The repository URL contains an unsafe path")
    parts = [part for part in decoded_path.split("/") if part]
    if parsed.hostname in {"github.com", "codeberg.org"} and len(parts) < 2:
        raise ValueError("The repository URL must contain an owner and repository")
    if parsed.hostname in {"zenodo.org", "www.zenodo.org"} and (
        len(parts) < 2 or parts[0] not in {"record", "records"} or not parts[1].isdigit()
    ):
        raise ValueError("The Zenodo URL must identify a published record")
    return value.strip()


def validate_notebooks(value: object) -> list[str]:
    if not isinstance(value, list) or not value or len(value) > MAX_NOTEBOOKS:
        raise ValueError(f"Provide between 1 and {MAX_NOTEBOOKS} notebook paths")
    validated = []
    for item in value:
        if not isinstance(item, str) or len(item) > 4096:
            raise ValueError("Every notebook path must be a string")
        normalized = item.strip().replace("\\", "/")
        path = PurePosixPath(normalized)
        if not normalized.endswith(".ipynb") or path.is_absolute() or ".." in path.parts:
            raise ValueError("An unsafe notebook path was rejected")
        validated.append(normalized)
    return validated


def _split_list(value: str | None) -> list[str]:
    if not value:
        return []
    return [item.strip() for item in value.split(";") if item.strip()]


def _platform_url(platform: str, repository: str) -> str:
    if platform == "github":
        return f"https://github.com/{repository}"
    if platform == "codeberg":
        return f"https://codeberg.org/{repository}"
    if platform == "zenodo":
        return f"https://zenodo.org/records/{repository}"
    return repository


def list_pipeline_repositories() -> list[dict]:
    """Read real, already-executed repositories straight from the pipeline's
    own SQLite database (data/output/db/db.sqlite) — read-only, no Docker
    required. This is the actual pipeline data, distinct from the website's
    own D1-backed "imported repository" workspace."""
    db_path = MAIN_DB_DIR / "db.sqlite"
    if not db_path.is_file():
        return []

    connection = sqlite3.connect(f"{db_path.resolve().as_uri()}?mode=ro", uri=True, timeout=5)
    connection.row_factory = sqlite3.Row
    try:
        repo_rows = connection.execute(
            """
            SELECT
                r.id, r.repository, r.platform, r.notebooks_count,
                m.title, m.description, m.authors, m.license, m.doi, m.keywords,
                (SELECT run_status FROM repository_runs WHERE repository_id = r.id ORDER BY id DESC LIMIT 1) AS last_status,
                (SELECT started_at FROM repository_runs WHERE repository_id = r.id ORDER BY id DESC LIMIT 1) AS last_started_at,
                (SELECT finished_at FROM repository_runs WHERE repository_id = r.id ORDER BY id DESC LIMIT 1) AS last_finished_at,
                (SELECT COUNT(*) FROM repository_runs WHERE repository_id = r.id) AS run_count
            FROM repositories r
            LEFT JOIN repository_metadata m ON m.repo_id = r.id
            ORDER BY r.id DESC
            """
        ).fetchall()

        notebook_rows = connection.execute(
            """
            SELECT
                n.id, n.repository_id, n.name, n.language,
                nc.final_category, nc.rule_category, nc.llm_category,
                nc.agreement_status, nc.needs_human_review,
                ne.execution_status, ne.execution_duration,
                nrm.reproducibility_score
            FROM notebooks n
            LEFT JOIN (
                SELECT notebook_id, final_category, rule_category, llm_category, agreement_status, needs_human_review
                FROM notebook_classifications WHERE id IN (SELECT MAX(id) FROM notebook_classifications GROUP BY notebook_id)
            ) nc ON nc.notebook_id = n.id
            LEFT JOIN (
                SELECT notebook_id, execution_status, execution_duration
                FROM notebook_executions WHERE id IN (SELECT MAX(id) FROM notebook_executions GROUP BY notebook_id)
            ) ne ON ne.notebook_id = n.id
            LEFT JOIN (
                SELECT notebook_id, reproducibility_score
                FROM notebook_reproducibility_metrics WHERE id IN (SELECT MAX(id) FROM notebook_reproducibility_metrics GROUP BY notebook_id)
            ) nrm ON nrm.notebook_id = n.id
            ORDER BY n.repository_id, n.id
            """
        ).fetchall()
    finally:
        connection.close()

    notebooks_by_repo: dict[int, list[dict]] = {}
    for row in notebook_rows:
        notebooks_by_repo.setdefault(row["repository_id"], []).append(
            {
                "id": str(row["id"]),
                "name": row["name"],
                "language": row["language"] or "Python",
                "finalCategory": row["final_category"],
                "ruleCategory": row["rule_category"],
                "llmCategory": row["llm_category"],
                "agreement": row["agreement_status"],
                "needsHumanReview": bool(row["needs_human_review"]),
                "executionStatus": row["execution_status"],
                "executionDuration": row["execution_duration"],
                "reproducibilityScore": row["reproducibility_score"],
            }
        )

    repositories = []
    for row in repo_rows:
        platform = row["platform"] or "github"
        repo_notebooks = notebooks_by_repo.get(row["id"], [])
        scores = [n["reproducibilityScore"] for n in repo_notebooks if n["reproducibilityScore"] is not None]
        repositories.append(
            {
                "id": str(row["id"]),
                "name": row["repository"],
                "platform": platform,
                "url": _platform_url(platform, row["repository"]),
                "title": row["title"] or row["repository"],
                "description": row["description"] or "",
                "authors": _split_list(row["authors"]),
                "license": row["license"] or "",
                "doi": row["doi"] or None,
                "keywords": _split_list(row["keywords"]),
                "notebookCount": row["notebooks_count"] if row["notebooks_count"] is not None else len(repo_notebooks),
                "lastRunStatus": row["last_status"],
                "lastRunStartedAt": row["last_started_at"],
                "lastRunFinishedAt": row["last_finished_at"],
                "runCount": row["run_count"],
                "averageScorePercent": round(sum(scores) / len(scores) * 100) if scores else None,
                "notebooks": repo_notebooks,
            }
        )
    return repositories


def _repo_dir_name(repository_id: int, repository_path: str) -> str:
    basename = repository_path.rstrip("/").split("/")[-1]
    if basename.endswith(".git"):
        basename = basename[:-4]
    return f"{repository_id}_{basename}"


def _ensure_repo_acquired(repository_id: int, repo_url: str) -> Path:
    """Makes sure this repository's real source is cloned/downloaded on disk
    and returns its directory — reusing classify-only.sh's acquisition step
    (with classification skipped) so the exact same validate+clone/download
    logic the rest of the pipeline relies on is the only thing that ever
    creates these directories. Cheap no-op if already acquired."""
    db_path = MAIN_DB_DIR / "db.sqlite"
    connection = sqlite3.connect(f"{db_path.resolve().as_uri()}?mode=ro", uri=True, timeout=5)
    try:
        row = connection.execute("SELECT repository FROM repositories WHERE id = ?", (repository_id,)).fetchone()
    finally:
        connection.close()
    if row is None:
        raise ValueError("Repository not found in the pipeline database")

    repo_dir = DATA_OUTPUT_ROOT / "cloned_repos" / _repo_dir_name(repository_id, row[0])
    if repo_dir.is_dir():
        return repo_dir

    job_path = f"/tmp/notebookfair-acquire-{uuid.uuid4().hex}"
    project_path = _wsl_mount_path(ROOT)
    repos_dir_override = _wsl_mount_path(DATA_OUTPUT_ROOT / "cloned_repos")
    script = (
        f"mkdir -p {shlex.quote(job_path)} && cd {shlex.quote(project_path)} && "
        f"WEB_REPO_URL={shlex.quote(repo_url)} WEB_JOB_DIR={shlex.quote(job_path)} "
        f"REPOS_DIR_OVERRIDE={shlex.quote(repos_dir_override)} "
        "SKIP_CLASSIFY=true bash pipeline/classify-only.sh"
    )
    command = ["wsl", "bash", "-lc", script] if platform.system() == "Windows" else ["bash", "-lc", script]
    result = subprocess.run(command, capture_output=True, text=True, timeout=180, encoding="utf-8", errors="replace")
    subprocess.run(
        (["wsl", "bash", "-lc", f"rm -rf {shlex.quote(job_path)}"] if platform.system() == "Windows"
         else ["bash", "-lc", f"rm -rf {shlex.quote(job_path)}"]),
        capture_output=True, timeout=30, check=False,
    )
    if result.returncode != 0 or not repo_dir.is_dir():
        log_tail = "\n".join((result.stdout or "").splitlines()[-20:])
        raise ValueError(f"Could not acquire this repository first: {log_tail or 'unknown error'}")
    return repo_dir


def attach_notebook_to_repository(repository_id: int, filename: str, content_text: str) -> dict:
    """Uploads a notebook from the user's computer straight into an existing,
    real repository — the repo must already be in the pipeline database.
    Acquires the repo first (clone/download) if it has never been run, so
    the file always lands inside a genuine, git-managed working copy rather
    than a loose directory that would later conflict with a real clone."""
    if not filename or "/" in filename or "\\" in filename or not filename.lower().endswith(".ipynb"):
        raise ValueError("Choose a single .ipynb file.")
    try:
        notebook_json = json.loads(content_text)
    except json.JSONDecodeError as error:
        raise ValueError(f"That file is not valid JSON, so it can't be a real .ipynb: {error}") from error
    if not isinstance(notebook_json, dict) or "cells" not in notebook_json:
        raise ValueError("That file doesn't look like a Jupyter notebook (no 'cells').")

    db_path = MAIN_DB_DIR / "db.sqlite"
    connection = sqlite3.connect(f"{db_path.resolve().as_uri()}?mode=ro", uri=True, timeout=5)
    try:
        row = connection.execute("SELECT repository, platform FROM repositories WHERE id = ?", (repository_id,)).fetchone()
    finally:
        connection.close()
    if row is None:
        raise ValueError("Repository not found in the pipeline database")
    repo_url = _platform_url(row[1] or "github", row[0])

    repo_dir = _ensure_repo_acquired(repository_id, repo_url)

    safe_name = re.sub(r"[^A-Za-z0-9._-]+", "_", Path(filename).stem).strip("_")[:80] or "notebook"
    notebook_filename = f"{safe_name}.ipynb"
    target = repo_dir / notebook_filename
    suffix = 2
    while target.exists():
        notebook_filename = f"{safe_name}-{suffix}.ipynb"
        target = repo_dir / notebook_filename
        suffix += 1
    target.write_text(content_text, encoding="utf-8")

    connection = sqlite3.connect(str(db_path), timeout=10)
    try:
        connection.execute(
            "INSERT INTO notebooks (repository_id, name, language) VALUES (?, ?, 'python')",
            (repository_id, notebook_filename),
        )
        connection.execute(
            "UPDATE repositories SET notebooks_count = (SELECT COUNT(*) FROM notebooks WHERE repository_id = ?) WHERE id = ?",
            (repository_id, repository_id),
        )
        connection.commit()
    finally:
        connection.close()

    return {"repositoryId": repository_id, "notebookFilename": notebook_filename}


def progress_from_line(line: str, completed_notebooks: int, total: int) -> tuple[int, str, str, int]:
    """Parses one pipeline log line into (progress%, stage, message,
    completed_notebooks). Shared between the Docker-sandboxed JobManager and
    the direct/WSL DirectRerunManager so both surface the same live progress
    in the UI."""
    if True:
        if "[REPO] Cloning" in line:
            return 10, "Acquiring repository", "Cloning the repository in an isolated container.", completed_notebooks
        if "[ZENODO]" in line:
            return 10, "Acquiring repository", "Downloading the Zenodo record in an isolated container.", completed_notebooks
        if "[REPO] Notebooks:" in line:
            match = re.search(r"python=(\d+)", line)
            count = int(match.group(1)) if match else total
            noun = "notebook" if count == 1 else "notebooks"
            return 15, "Acquiring repository", f"Repository acquired — {count} Python {noun} ready.", completed_notebooks
        if "[CLASSIFICATION] Classifying:" in line:
            name = line.split("Classifying:", 1)[-1].strip()
            return 20, "Classifying notebooks", f"Inspecting {name or 'the notebook'} to detect its category.", completed_notebooks
        if "[CLASSIFICATION] Result stored" in line:
            return 24, "Classifying notebooks", "Classification saved. Moving to the next notebook.", completed_notebooks
        if "[REQUIREMENT] Processing requirements" in line:
            return 26, "Resolving dependencies", "Looking for a requirements file in the repository.", completed_notebooks
        if "[REQUIREMENT] Extracting imports" in line:
            return 30, "Resolving dependencies", "Extracting the notebook's code to detect its imports.", completed_notebooks
        if "[REQUIREMENT] Added external library" in line:
            library = line.rsplit(":", 1)[-1].strip()
            return 33, "Resolving dependencies", f"Found dependency: {library or 'unknown package'}.", completed_notebooks
        if "Final requirements.txt created with" in line:
            match = re.search(r"with\s+(\d+)\s+packages", line)
            count = int(match.group(1)) if match else 0
            noun = "package" if count == 1 else "packages"
            return 38, "Resolving dependencies", f"Dependency discovery complete — {count} {noun} found.", completed_notebooks
        if "[REQUIREMENT]" in line:
            return 36, "Resolving dependencies", "Building the notebook's requirements file.", completed_notebooks
        if "[PYENV] Installing Python" in line:
            version = line.split("Installing Python", 1)[-1].split("(", 1)[0].strip()
            return 42, "Creating environment", f"Installing Python {version or 'runtime'} — this can take several minutes the first time.", completed_notebooks
        if "[PYENV] Creating venv" in line:
            return 50, "Creating environment", "Creating an isolated virtual environment.", completed_notebooks
        if "[PYENV] Upgrading pip" in line:
            return 52, "Creating environment", "Installing Jupyter and nbconvert into the environment.", completed_notebooks
        if "[PYENV] Installing: " in line:
            package = line.split("Installing:", 1)[-1].strip()
            return 54, "Installing dependencies", f"Installing {package or 'a package'}.", completed_notebooks
        if "[PYENV] ✓ " in line:
            package = line.split("✓", 1)[-1].strip()
            return 54, "Installing dependencies", f"{package or 'Package'} passed installation.", completed_notebooks
        if "[PYENV] Environment ready." in line:
            version = line.split("Python:", 1)[-1].strip() if "Python:" in line else "Python"
            return 55, "Creating environment", f"The isolated environment passed setup with {version}.", completed_notebooks
        if "[PYENV] Executing notebook:" in line:
            name = line.split("Executing notebook:", 1)[-1].strip()
            progress = 55 + round((completed_notebooks / max(total, 1)) * 35)
            return progress, "Executing notebooks", f"Notebook {completed_notebooks + 1} of {total} is running: {name}.", completed_notebooks
        if "[PYENV] Notebook executed successfully:" in line:
            return -1, "", "", completed_notebooks
        if "[PYENV] Notebook executed with errors:" in line:
            return -1, "", "", completed_notebooks
        if line.startswith("SUCCESS|") or line.startswith("SUCCESS_WITH_ERRORS|") or line.startswith("EXEC_FAIL|"):
            completed_notebooks += 1
            progress = 55 + round((completed_notebooks / max(total, 1)) * 35)
            return progress, "Executing notebooks", f"Completed {completed_notebooks} of {total} notebooks.", completed_notebooks
        if "[NOTEBOOK] ID=" in line and "path=" in line:
            name = line.split("path=", 1)[-1].strip()
            return 94, "Comparing outputs", f"Comparing original and executed outputs for {name}.", completed_notebooks
        if "[NOTEBOOK] Comparing outputs" in line:
            return 92, "Comparing outputs", "Calculating reproducibility results.", completed_notebooks
        return -1, "", "", completed_notebooks


class JobManager:
    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.image_lock = threading.Lock()
        self.image_ready = False
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="pipeline-job")
        self.processes: dict[str, tuple[subprocess.Popen, str]] = {}
        MAIN_DB_DIR.mkdir(parents=True, exist_ok=True)
        JOBS_ROOT.mkdir(parents=True, exist_ok=True)
        self._recover_interrupted_jobs()

    def _recover_interrupted_jobs(self) -> None:
        for state_path in JOBS_ROOT.glob("*/state.json"):
            try:
                state = json.loads(state_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if state.get("status") not in TERMINAL_STATES:
                state.update(
                    status="failed",
                    stage="Interrupted",
                    message="The local pipeline runner stopped before this job finished.",
                    error="Runner interrupted",
                    finishedAt=utc_now(),
                )
                atomic_json(state_path, state)

    def _job_dir(self, job_id: str) -> Path:
        if not re.fullmatch(r"[0-9a-f]{32}", job_id):
            raise KeyError(job_id)
        directory = (JOBS_ROOT / job_id).resolve()
        directory.relative_to(JOBS_ROOT.resolve())
        return directory

    def _read(self, job_id: str) -> dict:
        state_path = self._job_dir(job_id) / "state.json"
        if not state_path.is_file():
            raise KeyError(job_id)
        return json.loads(state_path.read_text(encoding="utf-8"))

    def _write(self, job_id: str, **changes: object) -> dict:
        with self.lock:
            state = self._read(job_id)
            state.update(changes, updatedAt=utc_now())
            atomic_json(self._job_dir(job_id) / "state.json", state)
            return state

    def create(self, payload: dict) -> dict:
        repository_id = payload.get("repositoryId")
        if not isinstance(repository_id, str) or not re.fullmatch(r"[0-9a-f-]{36}", repository_id):
            raise ValueError("A valid workspace repository ID is required")
        repository_url = validate_repository_url(payload.get("url"))
        notebooks = validate_notebooks(payload.get("notebooks"))
        if not self._docker_available():
            raise RuntimeError("Analysis cannot start because Docker is unavailable. Start Docker, then retry analysis from the repository page.")
        job_id = uuid.uuid4().hex
        job_dir = self._job_dir(job_id)
        job_dir.mkdir(parents=True, exist_ok=False)
        state = {
            "id": job_id,
            "repositoryId": repository_id,
            "repositoryUrl": repository_url,
            "notebookCount": len(notebooks),
            "status": "queued",
            "stage": "Queued",
            "progress": 1,
            "message": "Waiting for the isolated pipeline runner.",
            "error": None,
            "createdAt": utc_now(),
            "updatedAt": utc_now(),
            "startedAt": None,
            "finishedAt": None,
        }
        atomic_json(job_dir / "request.json", {"url": repository_url, "notebooks": notebooks})
        atomic_json(job_dir / "state.json", state)
        self.executor.submit(self._run, job_id)
        return state

    def get(self, job_id: str) -> dict:
        with self.lock:
            state = self._read(job_id)
        log_path = self._job_dir(job_id) / "runner.log"
        if log_path.is_file():
            lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
            state["log"] = lines[-MAX_LOG_LINES:]
        else:
            state["log"] = []
        result_path = self._job_dir(job_id) / "result.json"
        if result_path.is_file():
            try:
                state["result"] = json.loads(result_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                state["result"] = None
        return state

    def cancel(self, job_id: str) -> dict:
        state = self._read(job_id)
        if state.get("status") in TERMINAL_STATES:
            return self.get(job_id)
        process_entry = self.processes.get(job_id)
        if process_entry:
            process, container_name = process_entry
            subprocess.run(
                ["docker", "stop", "--time", "10", container_name],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=20,
                check=False,
            )
            if process.poll() is None:
                process.terminate()
        self._write(
            job_id,
            status="cancelled",
            stage="Cancelled",
            progress=state.get("progress", 0),
            message="The pipeline run was cancelled.",
            finishedAt=utc_now(),
        )
        return self.get(job_id)

    def _docker_available(self) -> bool:
        try:
            result = subprocess.run(
                ["docker", "info", "--format", "{{.ServerVersion}}"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=5,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):  # docker not installed or daemon hung
            return False
        return result.returncode == 0

    def health(self) -> dict:
        available = self._docker_available()
        return {
            "status": "ok" if available else "unavailable",
            "docker": available,
            "image": IMAGE_NAME,
        }

    def _ensure_volume_ownership(self, log_handle) -> None:
        # The named volume can end up owned by root if it was ever created or
        # touched by a different container/user (for example, an earlier
        # interrupted run). The pipeline container runs as the unprivileged
        # jovyan user (uid 1000) and cannot fix this itself, so repair
        # ownership here, as root, before every run. This is cheap and
        # idempotent — safe to run even when ownership is already correct.
        subprocess.run(
            [
                "docker", "run", "--rm", "--user", "root",
                "--volume", "notebookfair-pyenv-versions:/mnt/pyenv-versions",
                IMAGE_NAME, "chown", "-R", "1000:1000", "/mnt/pyenv-versions",
            ],
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            timeout=30,
            check=False,
        )

    def _ensure_image(self, job_id: str, log_handle) -> None:
        with self.image_lock:
            if self.image_ready:
                return
            self._write(
                job_id,
                status="building",
                stage="Preparing runner",
                progress=4,
                message="Building or refreshing the isolated pipeline image. This only happens once per runner start.",
            )
            result = subprocess.run(
                ["docker", "build", "--tag", IMAGE_NAME, "--file", str(ROOT / "binder" / "Dockerfile"), str(ROOT)],
                stdout=log_handle,
                stderr=subprocess.STDOUT,
                timeout=1800,
                check=False,
            )
            if result.returncode != 0:
                raise RuntimeError("The Docker pipeline image could not be built")
            self.image_ready = True

    def _progress_from_line(self, line: str, completed_notebooks: int, total: int) -> tuple[int, str, str, int]:
        return progress_from_line(line, completed_notebooks, total)


    def _run(self, job_id: str) -> None:
        job_dir = self._job_dir(job_id)
        log_path = job_dir / "runner.log"
        try:
            request = json.loads((job_dir / "request.json").read_text(encoding="utf-8"))
            if not self._docker_available():
                raise RuntimeError("Docker Desktop is not running")
            self._write(
                job_id,
                status="preparing",
                stage="Preparing runner",
                progress=3,
                message="Checking the isolated pipeline environment.",
                startedAt=utc_now(),
            )
            with log_path.open("a", encoding="utf-8", buffering=1) as log_handle:
                self._ensure_image(job_id, log_handle)
                self._ensure_volume_ownership(log_handle)
                container_name = f"notebookfair-{job_id[:20]}"
                environment = os.environ.copy()
                environment.update(
                    WEB_REPO_URL=request["url"],
                    WEB_NOTEBOOK_PATHS=";".join(request["notebooks"]),
                    WEB_JOB_DIR="/job",
                    CLASSIFICATION_ENABLED="true",
                    CLASSIFICATION_RULE_ONLY=os.environ.get("CLASSIFICATION_RULE_ONLY", "true"),
                    PYTHONUNBUFFERED="1",
                )
                # Fedora and other SELinux hosts require labels on these
                # specific bind mounts before the container can read them.
                mount_label = ",z" if Path("/sys/fs/selinux/enforce").exists() else ""
                command = [
                    "docker", "run", "--rm", "--name", container_name,
                    "--memory", os.environ.get("NOTEBOOKFAIR_JOB_MEMORY", "4g"),
                    "--cpus", os.environ.get("NOTEBOOKFAIR_JOB_CPUS", "2"),
                    "--pids-limit", "512",
                    "--security-opt", "no-new-privileges",
                    "--cap-drop", "ALL",
                    "--volume", f"{ROOT}:/workspace:ro{mount_label}",
                    "--volume", f"{MAIN_DB_DIR}:/workspace/data/output/db:rw{mount_label}",
                    "--volume", f"{job_dir}:/job:rw{mount_label}",
                    "--volume", "notebookfair-pyenv-versions:/home/jovyan/.pyenv/versions",
                    "--tmpfs", "/job/venvs:rw,exec,nosuid,nodev,size=2g,uid=1000,gid=1000,mode=0755",
                    "--workdir", "/job",
                    "--env", "WEB_REPO_URL",
                    "--env", "WEB_NOTEBOOK_PATHS",
                    "--env", "WEB_JOB_DIR",
                    "--env", "CLASSIFICATION_ENABLED",
                    "--env", "CLASSIFICATION_RULE_ONLY",
                    "--env", "PYTHONUNBUFFERED",
                ]
                for secret_name in ("GITHUB_TOKEN", "CODEBERG_TOKEN", "ZENODO_TOKEN"):
                    if environment.get(secret_name):
                        command.extend(("--env", secret_name))
                command.extend((IMAGE_NAME, "bash", "/workspace/pipeline/web-run.sh"))

                self._write(
                    job_id,
                    status="running",
                    stage="Starting pipeline",
                    progress=8,
                    message="The repository is entering the reproducibility pipeline.",
                )
                process = subprocess.Popen(
                    command,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    env=environment,
                )
                self.processes[job_id] = (process, container_name)
                timed_out = threading.Event()

                def stop_after_timeout() -> None:
                    timed_out.set()
                    subprocess.run(
                        ["docker", "stop", "--time", "10", container_name],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        timeout=20,
                        check=False,
                    )

                timeout_timer = threading.Timer(JOB_TIMEOUT_SECONDS, stop_after_timeout)
                timeout_timer.daemon = True
                timeout_timer.start()
                completed_notebooks = 0
                try:
                    assert process.stdout is not None
                    for line in process.stdout:
                        log_handle.write(line)
                        progress, stage, message, completed_notebooks = self._progress_from_line(
                            line.rstrip(), completed_notebooks, len(request["notebooks"])
                        )
                        if progress >= 0:
                            current = self._read(job_id)
                            if current.get("status") == "cancelled":
                                return
                            self._write(job_id, progress=progress, stage=stage, message=message)
                    return_code = process.wait()
                finally:
                    timeout_timer.cancel()
                if timed_out.is_set():
                    raise subprocess.TimeoutExpired(command, JOB_TIMEOUT_SECONDS)
                if self._read(job_id).get("status") == "cancelled":
                    return
                result_path = job_dir / "result.json"
                if not result_path.is_file():
                    details = log_path.read_text(encoding="utf-8", errors="replace").strip().splitlines()
                    reason = details[-1][-400:] if details else "No diagnostic output was recorded."
                    raise RuntimeError(f"The pipeline stopped before saving results (exit code {return_code}): {reason}")
                result = json.loads(result_path.read_text(encoding="utf-8"))
                status = result.get("status")
                if status not in {"succeeded", "partial", "failed"}:
                    status = "failed"
                messages = {
                    "succeeded": "The pipeline completed and all results are ready.",
                    "partial": "The pipeline completed with notebook warnings or failures.",
                    "failed": result.get("error") or "The pipeline could not complete this repository.",
                }
                self._write(
                    job_id,
                    status=status,
                    stage="Completed" if status != "failed" else "Failed",
                    progress=100,
                    message=messages[status],
                    error=result.get("error") if status == "failed" else None,
                    finishedAt=utc_now(),
                )
        except subprocess.TimeoutExpired:
            self.cancel(job_id)
            self._write(
                job_id,
                status="failed",
                stage="Timed out",
                message="The pipeline exceeded its two-hour safety limit.",
                error="Pipeline timed out",
                finishedAt=utc_now(),
            )
        except Exception as error:  # keep the local service alive after job failures
            self._write(
                job_id,
                status="failed",
                stage="Failed",
                message=str(error),
                error=str(error),
                finishedAt=utc_now(),
            )
        finally:
            self.processes.pop(job_id, None)


def _wsl_mount_path(path: Path) -> str:
    """Convert a native Windows path to its WSL /mnt/<drive>/... equivalent.
    On non-Windows hosts the path is already usable as-is."""
    resolved = path.resolve()
    if platform.system() != "Windows":
        return str(resolved)
    drive = resolved.drive.rstrip(":").lower()
    rest = str(resolved)[len(resolved.drive):].replace("\\", "/").lstrip("/")
    return f"/mnt/{drive}/{rest}"


class DirectRerunManager:
    """Reruns a single real pipeline notebook directly on the host (via WSL on
    Windows), bypassing the Docker-sandboxed JobManager above. This is what
    powers the "Rerun" button for notebooks already sitting in the pipeline's
    own SQLite database — it writes results into that same real database,
    with no Docker Desktop dependency."""

    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.jobs: dict[str, dict] = {}
        self.state_dir = DATA_OUTPUT_ROOT / "direct-runs"
        self.state_dir.mkdir(parents=True, exist_ok=True)
        for saved in self.state_dir.glob("*.json"):
            try:
                value = json.loads(saved.read_text(encoding="utf-8"))
                if value.get("status") == "running":
                    value.update(status="failed", message="The analysis service stopped before this run finished. Run it again.", error="Run interrupted", updatedAt=utc_now())
                    atomic_json(saved, value)
                self.jobs[value["id"]] = value
            except (OSError, ValueError, KeyError):
                continue
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="direct-rerun")

    def _lookup(self, repository_id: int, notebook_id: int) -> tuple[str, str]:
        db_path = MAIN_DB_DIR / "db.sqlite"
        connection = sqlite3.connect(f"{db_path.resolve().as_uri()}?mode=ro", uri=True, timeout=5)
        try:
            repo = connection.execute(
                "SELECT repository, platform FROM repositories WHERE id = ?", (repository_id,)
            ).fetchone()
            if repo is None:
                raise ValueError("Repository not found in the pipeline database")
            notebook = connection.execute(
                "SELECT name FROM notebooks WHERE id = ? AND repository_id = ?",
                (notebook_id, repository_id),
            ).fetchone()
            if notebook is None:
                raise ValueError("Notebook not found in the pipeline database")
        finally:
            connection.close()
        repository_path, platform_name = repo
        return _platform_url(platform_name or "github", repository_path), notebook[0]

    def _lookup_repository(self, repository_id: int) -> tuple[str, str]:
        db_path = MAIN_DB_DIR / "db.sqlite"
        connection = sqlite3.connect(f"{db_path.resolve().as_uri()}?mode=ro", uri=True, timeout=5)
        try:
            repo = connection.execute(
                "SELECT repository, platform FROM repositories WHERE id = ?", (repository_id,)
            ).fetchone()
            if repo is None:
                raise ValueError("Repository not found in the pipeline database")
            rows = connection.execute(
                "SELECT name FROM notebooks WHERE repository_id = ? ORDER BY id", (repository_id,)
            ).fetchall()
        finally:
            connection.close()
        if not rows:
            raise ValueError("This repository has no notebooks to run yet.")
        repository_path, platform_name = repo
        notebook_paths = ";".join(row[0] for row in rows)
        return _platform_url(platform_name or "github", repository_path), notebook_paths

    def start_repository(self, repository_id: int) -> dict:
        repo_url, notebook_paths = self._lookup_repository(repository_id)
        return self._start(repository_id, None, repo_url, notebook_paths)

    def start(self, repository_id: int, notebook_id: int) -> dict:
        repo_url, notebook_path = self._lookup(repository_id, notebook_id)
        return self._start(repository_id, notebook_id, repo_url, notebook_path)

    def _start(self, repository_id: int, notebook_id: int | None, repo_url: str, notebook_path: str) -> dict:
        job_id = uuid.uuid4().hex
        notebook_count = notebook_path.count(";") + 1
        starting_message = (
            f"Rerunning {notebook_path}…" if notebook_id is not None
            else f"Rerunning {notebook_count} notebook{'s' if notebook_count != 1 else ''}…"
        )
        state = {
            "id": job_id,
            "repositoryId": str(repository_id),
            "notebookId": str(notebook_id) if notebook_id is not None else None,
            "notebookPath": notebook_path,
            "status": "running",
            "stage": "Starting",
            "progress": 1,
            "message": starting_message,
            "error": None,
            "log": [],
            "createdAt": utc_now(),
            "updatedAt": utc_now(),
            "result": None,
        }
        with self.lock:
            self.jobs[job_id] = state
            atomic_json(self.state_dir / f"{job_id}.json", state)
        self.executor.submit(self._run, job_id, repo_url, notebook_path, notebook_count)
        return state

    def _update(self, job_id: str, **changes: object) -> None:
        with self.lock:
            state = self.jobs.get(job_id)
            if state is None:
                return
            state.update(changes, updatedAt=utc_now())
            atomic_json(self.state_dir / f"{job_id}.json", state)

    @staticmethod
    def _wsl_command(script: str) -> list[str]:
        return ["wsl", "bash", "-lc", script] if platform.system() == "Windows" else ["bash", "-lc", script]

    def _run(self, job_id: str, repo_url: str, notebook_path: str, notebook_total: int = 1) -> None:
        # Scratch work (git clone, pyenv venv, `pip install jupyter`) lives
        # under WSL's own native filesystem (/tmp), never under /mnt/c/. That
        # Windows-mounted path is a 9P network-style mount from WSL's side —
        # installing a large dependency tree there (many small files) can
        # take 10-50x longer than native ext4 and effectively hangs. Only the
        # real pipeline database write (hardcoded inside web-run.sh to the
        # project's own data/output/db/db.sqlite) needs to touch /mnt/c/ at
        # all; everything else here is disposable per-job scratch space.
        job_path = f"/tmp/notebookfair-direct-{job_id}"
        try:
            project_path = _wsl_mount_path(ROOT)
            run_script = (
                f"mkdir -p {shlex.quote(job_path)} && cd {shlex.quote(project_path)} && "
                f"WEB_REPO_URL={shlex.quote(repo_url)} "
                f"WEB_NOTEBOOK_PATHS={shlex.quote(notebook_path)} "
                f"WEB_JOB_DIR={shlex.quote(job_path)} "
                "CLASSIFICATION_ENABLED=true CLASSIFICATION_RULE_ONLY=true "
                "bash pipeline/web-run.sh"
            )
            process = subprocess.Popen(
                self._wsl_command(run_script), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace", bufsize=1,
            )
            log_lines: list[str] = []
            completed_notebooks = 0
            timed_out = False
            deadline = time.monotonic() + 1800
            assert process.stdout is not None
            for line in process.stdout:
                log_lines.append(line.rstrip("\n"))
                progress, stage, message, completed_notebooks = progress_from_line(
                    line.rstrip("\n"), completed_notebooks, notebook_total
                )
                if progress >= 0:
                    self._update(job_id, stage=stage, progress=progress, message=message, log=log_lines[-80:])
                if time.monotonic() > deadline:
                    timed_out = True
                    process.terminate()
                    break
            return_code = process.wait()
            if timed_out:
                raise subprocess.TimeoutExpired(run_script, 1800)

            cat_result = subprocess.run(
                self._wsl_command(f"cat {shlex.quote(job_path)}/result.json 2>/dev/null"),
                capture_output=True, text=True, timeout=30, encoding="utf-8", errors="replace",
            )

            if cat_result.stdout.strip():
                payload = json.loads(cat_result.stdout)
                self._update(
                    job_id,
                    status=payload.get("status", "failed"),
                    stage="Complete",
                    progress=100,
                    message="Rerun complete.",
                    error=payload.get("error"),
                    result=payload,
                )
            else:
                log_tail = "\n".join(log_lines[-40:])
                self._update(
                    job_id,
                    status="failed",
                    message="The rerun did not produce a result.",
                    error=log_tail or f"The rerun exited with code {return_code}",
                )
        except subprocess.TimeoutExpired:
            self._update(job_id, status="failed", message="The rerun timed out.", error="Timed out after 30 minutes")
        except json.JSONDecodeError as error:
            self._update(job_id, status="failed", message="The rerun result could not be parsed.", error=str(error))
        except Exception as error:  # keep the server alive after any single rerun failure
            self._update(job_id, status="failed", message=str(error), error=str(error))
        finally:
            subprocess.run(
                self._wsl_command(f"rm -rf {shlex.quote(job_path)}"),
                capture_output=True, timeout=30, check=False,
            )

    def get(self, job_id: str) -> dict:
        with self.lock:
            state = self.jobs.get(job_id)
        if state is None:
            raise KeyError(job_id)
        return state


class ClassifyOnlyManager:
    """Reclassifies one notebook without a full rerun — no dependency install,
    no pyenv, no execution. Much faster; used by the small "reclassify" action
    next to a notebook's classification badge."""

    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.jobs: dict[str, dict] = {}
        self.state_dir = DATA_OUTPUT_ROOT / "classify-runs"
        self.state_dir.mkdir(parents=True, exist_ok=True)
        for saved in self.state_dir.glob("*.json"):
            try:
                value = json.loads(saved.read_text(encoding="utf-8"))
                if value.get("status") == "running":
                    value.update(status="failed", message="The analysis service stopped before this run finished. Run it again.", error="Run interrupted", updatedAt=utc_now())
                    atomic_json(saved, value)
                self.jobs[value["id"]] = value
            except (OSError, ValueError, KeyError):
                continue
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="classify-only")

    def _lookup(self, repository_id: int, notebook_id: int) -> tuple[str, str]:
        db_path = MAIN_DB_DIR / "db.sqlite"
        connection = sqlite3.connect(f"{db_path.resolve().as_uri()}?mode=ro", uri=True, timeout=5)
        try:
            repo = connection.execute(
                "SELECT repository, platform FROM repositories WHERE id = ?", (repository_id,)
            ).fetchone()
            if repo is None:
                raise ValueError("Repository not found in the pipeline database")
            notebook = connection.execute(
                "SELECT name FROM notebooks WHERE id = ? AND repository_id = ?",
                (notebook_id, repository_id),
            ).fetchone()
            if notebook is None:
                raise ValueError("Notebook not found in the pipeline database")
        finally:
            connection.close()
        repository_path, platform_name = repo
        return _platform_url(platform_name or "github", repository_path), notebook[0]

    def _read_classification(self, notebook_id: int) -> dict | None:
        db_path = MAIN_DB_DIR / "db.sqlite"
        connection = sqlite3.connect(f"{db_path.resolve().as_uri()}?mode=ro", uri=True, timeout=5)
        connection.row_factory = sqlite3.Row
        try:
            row = connection.execute(
                """
                SELECT rule_category, llm_category, final_category, agreement_status, needs_human_review
                FROM notebook_classifications WHERE notebook_id = ? ORDER BY id DESC LIMIT 1
                """,
                (notebook_id,),
            ).fetchone()
        finally:
            connection.close()
        return dict(row) if row is not None else None

    def start(self, repository_id: int, notebook_id: int) -> dict:
        repo_url, notebook_path = self._lookup(repository_id, notebook_id)
        job_id = uuid.uuid4().hex
        state = {
            "id": job_id,
            "repositoryId": str(repository_id),
            "notebookId": str(notebook_id),
            "status": "running",
            "message": f"Reclassifying {notebook_path}…",
            "error": None,
            "createdAt": utc_now(),
            "updatedAt": utc_now(),
            "result": None,
        }
        with self.lock:
            self.jobs[job_id] = state
            atomic_json(self.state_dir / f"{job_id}.json", state)
        self.executor.submit(self._run, job_id, repo_url, notebook_path, notebook_id)
        return state

    def _update(self, job_id: str, **changes: object) -> None:
        with self.lock:
            state = self.jobs.get(job_id)
            if state is None:
                return
            state.update(changes, updatedAt=utc_now())
            atomic_json(self.state_dir / f"{job_id}.json", state)

    def _run(self, job_id: str, repo_url: str, notebook_path: str, notebook_id: int) -> None:
        job_path = f"/tmp/notebookfair-classify-{job_id}"
        try:
            project_path = _wsl_mount_path(ROOT)
            script = (
                f"mkdir -p {shlex.quote(job_path)} && cd {shlex.quote(project_path)} && "
                f"WEB_REPO_URL={shlex.quote(repo_url)} "
                f"WEB_NOTEBOOK_PATHS={shlex.quote(notebook_path)} "
                f"WEB_JOB_DIR={shlex.quote(job_path)} "
                "CLASSIFICATION_RULE_ONLY=true "
                "bash pipeline/classify-only.sh"
            )
            command = ["wsl", "bash", "-lc", script] if platform.system() == "Windows" else ["bash", "-lc", script]
            result = subprocess.run(
                command, capture_output=True, text=True, timeout=300, encoding="utf-8", errors="replace"
            )
            if result.returncode != 0:
                log_tail = "\n".join((result.stdout or "").splitlines()[-30:])
                self._update(
                    job_id, status="failed", message="Reclassification failed.",
                    error=log_tail or f"exit code {result.returncode}",
                )
                return
            classification = self._read_classification(notebook_id)
            if classification is None:
                self._update(job_id, status="failed", message="No classification result was produced.", error=None)
                return
            self._update(job_id, status="succeeded", message="Reclassification complete.", result=classification)
        except subprocess.TimeoutExpired:
            self._update(job_id, status="failed", message="Reclassification timed out.", error="Timed out after 5 minutes")
        except Exception as error:
            self._update(job_id, status="failed", message=str(error), error=str(error))
        finally:
            subprocess.run(
                (["wsl", "bash", "-lc", f"rm -rf {shlex.quote(job_path)}"] if platform.system() == "Windows"
                 else ["bash", "-lc", f"rm -rf {shlex.quote(job_path)}"]),
                capture_output=True, timeout=30, check=False,
            )

    def get(self, job_id: str) -> dict:
        with self.lock:
            state = self.jobs.get(job_id)
        if state is None:
            raise KeyError(job_id)
        return state


MANAGER = JobManager()
DIRECT_MANAGER = DirectRerunManager()
CLASSIFY_MANAGER = ClassifyOnlyManager()


class Handler(BaseHTTPRequestHandler):
    server_version = "NotebookFairPipeline/1.0"

    def log_message(self, format_string: str, *arguments: object) -> None:
        print(f"[pipeline-runner] {format_string % arguments}")

    def _json(self, status: HTTPStatus, payload: dict) -> None:
        encoded = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Cache-Control", "no-store")
        # Explicitly advertise HTTP/1.0 connection closure to worker runtimes,
        # which otherwise may reuse this socket for the next POST request.
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(encoded)

    def _job_id(self) -> str | None:
        match = re.fullmatch(r"/jobs/([0-9a-f]{32})(?:/cancel)?", urlparse(self.path).path)
        return match.group(1) if match else None

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/health":
            health = MANAGER.health()
            status = HTTPStatus.OK if health["status"] == "ok" else HTTPStatus.SERVICE_UNAVAILABLE
            self._json(status, health)
            return
        if path == "/knowledge-graph/build":
            from workspace_graph import status
            self._json(HTTPStatus.OK, status())
            return
        if path == "/knowledge-graph":
            try:
                from knowledge_graph import browse_graph
                query = parse_qs(urlparse(self.path).query)
                payload = browse_graph(
                    kind=query.get("kind", ["RepositoryResource"])[0],
                    query=query.get("q", [""])[0],
                    offset=int(query.get("offset", ["0"])[0]),
                    focus=query.get("focus", [None])[0],
                )
                self._json(HTTPStatus.OK, payload)
            except ImportError:
                self._json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": "Knowledge graph support is unavailable. Start the runner with the knowledge graph Python environment."})
            except FileNotFoundError as error:
                self._json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": str(error)})
            except ValueError as error:
                self._json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
            except Exception:
                self._json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": "The saved knowledge graph could not be read. Rebuild it, then retry."})
            return
        if path == "/workspace":
            try:
                from workspace_data import read_pipeline_workspace
                release = (parse_qs(urlparse(self.path).query).get("release") or [None])[0]
                self._json(HTTPStatus.OK, read_pipeline_workspace(release))
            except FileNotFoundError as error:
                self._json(HTTPStatus.NOT_FOUND, {"error": str(error)})
            except (OSError, ValueError, sqlite3.Error):
                self._json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": "Saved pipeline results could not be read."})
            return
        if path == "/releases":
            from study_data import list_releases
            self._json(HTTPStatus.OK, {"releases": list_releases()})
            return
        if path in ("/repository-detail", "/notebook-detail"):
            import study_data
            record_id = (parse_qs(urlparse(self.path).query).get("id") or [""])[0]
            try:
                reader = study_data.repository_detail if path == "/repository-detail" else study_data.notebook_detail
                self._json(HTTPStatus.OK, reader(record_id))
            except (LookupError, FileNotFoundError) as error:
                self._json(HTTPStatus.NOT_FOUND, {"error": str(error)})
            except ValueError as error:
                self._json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
            except (OSError, sqlite3.Error):
                self._json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": "Saved pipeline details could not be read."})
            return
        if path == "/repositories":
            try:
                self._json(HTTPStatus.OK, {"repositories": list_pipeline_repositories()})
            except sqlite3.Error as error:
                self._json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": f"The pipeline database could not be read: {error}"})
            return
        direct_match = re.fullmatch(r"/direct-rerun/([0-9a-f]{32})", path)
        if direct_match:
            try:
                self._json(HTTPStatus.OK, DIRECT_MANAGER.get(direct_match.group(1)))
            except KeyError:
                self._json(HTTPStatus.NOT_FOUND, {"error": "Job not found"})
            return
        classify_match = re.fullmatch(r"/classify-only/([0-9a-f]{32})", path)
        if classify_match:
            try:
                self._json(HTTPStatus.OK, CLASSIFY_MANAGER.get(classify_match.group(1)))
            except KeyError:
                self._json(HTTPStatus.NOT_FOUND, {"error": "Job not found"})
            return
        job_id = self._job_id()
        if job_id and path == f"/jobs/{job_id}":
            try:
                self._json(HTTPStatus.OK, MANAGER.get(job_id))
            except KeyError:
                self._json(HTTPStatus.NOT_FOUND, {"error": "Job not found"})
            return
        self._json(HTTPStatus.NOT_FOUND, {"error": "Not found"})

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        if path == "/knowledge-graph/build":
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if length <= 0 or length > 20 * 1024 * 1024:
                    raise ValueError("The workspace graph request is empty or too large.")
                from workspace_graph import start
                self._json(HTTPStatus.ACCEPTED, start(json.loads(self.rfile.read(length))))
            except (ValueError, TypeError) as error:
                self._json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
            except (ImportError, OSError):
                self._json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": "Graph support is unavailable or the graph could not be saved."})
            return
        if path == "/local-notebooks":
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if length <= 0 or length > MAX_UPLOAD_BYTES:
                    raise ValueError(f"The file is empty or larger than {MAX_UPLOAD_BYTES // (1024 * 1024)} MB.")
                payload = json.loads(self.rfile.read(length))
                if not isinstance(payload, dict):
                    raise ValueError("The upload request must be a JSON object")
                repository_id = int(payload.get("repositoryId"))
                filename = payload.get("filename")
                content = payload.get("content")
                if not isinstance(filename, str) or not isinstance(content, str):
                    raise ValueError("filename and content are required strings")
                self._json(HTTPStatus.CREATED, attach_notebook_to_repository(repository_id, filename, content))
            except (ValueError, TypeError, json.JSONDecodeError) as error:
                self._json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
            except sqlite3.Error as error:
                self._json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": f"The pipeline database could not be written: {error}"})
            return
        if path == "/direct-rerun":
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if length <= 0 or length > MAX_BODY_BYTES:
                    raise ValueError("The rerun request is empty or too large")
                payload = json.loads(self.rfile.read(length))
                if not isinstance(payload, dict):
                    raise ValueError("The rerun request must be a JSON object")
                repository_id = int(payload.get("repositoryId"))
                notebook_id = int(payload.get("notebookId"))
                self._json(HTTPStatus.ACCEPTED, DIRECT_MANAGER.start(repository_id, notebook_id))
            except (ValueError, TypeError, json.JSONDecodeError) as error:
                self._json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
            return
        if path == "/direct-rerun-repository":
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if length <= 0 or length > MAX_BODY_BYTES:
                    raise ValueError("The rerun request is empty or too large")
                payload = json.loads(self.rfile.read(length))
                if not isinstance(payload, dict):
                    raise ValueError("The rerun request must be a JSON object")
                repository_id = int(payload.get("repositoryId"))
                self._json(HTTPStatus.ACCEPTED, DIRECT_MANAGER.start_repository(repository_id))
            except (ValueError, TypeError, json.JSONDecodeError) as error:
                self._json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
            return
        if path == "/classify-only":
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if length <= 0 or length > MAX_BODY_BYTES:
                    raise ValueError("The reclassify request is empty or too large")
                payload = json.loads(self.rfile.read(length))
                if not isinstance(payload, dict):
                    raise ValueError("The reclassify request must be a JSON object")
                repository_id = int(payload.get("repositoryId"))
                notebook_id = int(payload.get("notebookId"))
                self._json(HTTPStatus.ACCEPTED, CLASSIFY_MANAGER.start(repository_id, notebook_id))
            except (ValueError, TypeError, json.JSONDecodeError) as error:
                self._json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
            return
        if path == "/jobs":
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if length <= 0 or length > MAX_BODY_BYTES:
                    raise ValueError("The job request is empty or too large")
                payload = json.loads(self.rfile.read(length))
                if not isinstance(payload, dict):
                    raise ValueError("The job request must be a JSON object")
                self._json(HTTPStatus.ACCEPTED, MANAGER.create(payload))
            except (ValueError, json.JSONDecodeError) as error:
                self._json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
            except RuntimeError as error:
                self._json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": str(error)})
            return
        job_id = self._job_id()
        if job_id and path == f"/jobs/{job_id}/cancel":
            try:
                self._json(HTTPStatus.OK, MANAGER.cancel(job_id))
            except KeyError:
                self._json(HTTPStatus.NOT_FOUND, {"error": "Job not found"})
            return
        self._json(HTTPStatus.NOT_FOUND, {"error": "Not found"})


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8788)
    arguments = parser.parse_args()
    if arguments.host not in {"127.0.0.1", "::1", "localhost"}:
        raise SystemExit("The pipeline runner may only bind to loopback")
    server = ThreadingHTTPServer((arguments.host, arguments.port), Handler)
    print(f"[pipeline-runner] listening on http://{arguments.host}:{arguments.port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        MANAGER.executor.shutdown(wait=False, cancel_futures=True)


if __name__ == "__main__":
    main()
