#!/usr/bin/env python3
"""Restricted SSH entry point for the existing stock_prod installation.

Install OUTSIDE the deployed source tree. Only check, diagnose, and
'deploy <commit-sha> <archive-sha256>' are accepted. Detailed output stays
in private server-side files, never in public GitHub Actions logs.
Deployment is privileged: only approve source revisions you trust.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time

APP = Path.home() / "stock-agent-platform"
STATE = Path.home() / ".local/state/stock-actions"
PROTECTED = {".git", ".env", "data", "models", "runtime_data", "runtime", "outputs",
             "logs", "external_repos", "local_app_config.json", "llm_prompt_dumps"}
SKIP_PARTS = {"node_modules", ".venv", "venv", "__pycache__", ".pytest_cache", "dist"}
MAX_ARCHIVE = 64 * 1024 * 1024
MAX_EXPANDED = 256 * 1024 * 1024
STATES = {"running", "exited", "restarting", "created", "paused", "dead", "removing"}
HEALTH = {"healthy", "unhealthy", "starting", "none"}


class Refused(Exception):
    pass


def parse_command(command: str) -> tuple[str, ...]:
    parts = tuple(command.split())
    if parts in {("check",), ("diagnose",)}:
        return parts
    if (len(parts) == 3 and parts[0] == "deploy"
            and re.fullmatch(r"[a-f0-9]{40}", parts[1])
            and re.fullmatch(r"[a-f0-9]{64}", parts[2])):
        return parts
    raise Refused("command_not_allowed")


def protected(name: str) -> bool:
    parts = PurePosixPath(name).parts
    return (not parts or parts[0] in PROTECTED or bool(set(parts) & SKIP_PARTS)
            or name == "deploy/server.env" or name.startswith("deploy/migration/")
            or name.endswith((".secret", ".pem", ".key")))


def unpack(archive: Path, target: Path) -> None:
    """Reject links, traversal, duplicate paths and oversized source packages."""
    with tarfile.open(archive, "r:gz") as source:
        members = source.getmembers()
        if len(members) > 20000:
            raise Refused("archive_too_many_files")
        total = 0
        seen: set[str] = set()
        for item in members:
            name = item.name
            path = PurePosixPath(name)
            if (path.is_absolute() or ".." in path.parts or "\\" in name
                    or "\x00" in name or not (item.isfile() or item.isdir())):
                raise Refused("unsafe_archive_path_or_link")
            if path.as_posix() != name.rstrip("/"):
                raise Refused("noncanonical_archive_path")
            if name in seen:
                raise Refused("duplicate_archive_path")
            seen.add(name)
            total += item.size
        if total > MAX_EXPANDED:
            raise Refused("archive_expanded_too_large")
        for item in members:
            if protected(item.name):
                continue
            destination = target / item.name
            if item.isdir():
                destination.mkdir(parents=True, exist_ok=True)
                continue
            destination.parent.mkdir(parents=True, exist_ok=True)
            handle = source.extractfile(item)
            if handle is None:
                raise Refused("archive_file_missing")
            with handle, destination.open("wb") as out:
                shutil.copyfileobj(handle, out)
            destination.chmod(0o755 if item.mode & 0o111 else 0o644)
    for required in ("docker-compose.prod.yml", "Dockerfile.compose", "requirements-compose-api.txt"):
        if not (target / required).is_file():
            raise Refused("source_layout_invalid")


def docker_compose(root: Path) -> list[str]:
    return ["docker", "compose", "-p", "stock_prod", "--project-directory", str(root),
            "-f", str(root / "docker-compose.prod.yml"),
            "--env-file", str(APP / "deploy/server.env")]


class Ops:
    def __init__(self, run: Path):
        self.run = run
        self.log = run / "private.log"
        self.stage = "preflight"

    def call(self, stage: str, command: list[str], *, timeout: int = 120,
             stdin=None, stdout=None, capture: bool = False) -> str:
        self.stage = stage
        print(f"stage={stage} status=started", flush=True)
        with self.log.open("ab") as log:
            log.write(f"\n--- {stage} ---\n".encode())
            result = subprocess.run(command, stdin=stdin, stdout=(subprocess.PIPE if capture else stdout or log),
                                    stderr=log, timeout=timeout, check=False)
            data = result.stdout or b"" if capture else b""
            if capture:
                log.write(data)
        if result.returncode:
            raise Refused(f"{stage}_failed")
        print(f"stage={stage} status=ok", flush=True)
        return data.decode("utf-8", errors="replace")

    def status(self) -> None:
        for service in ("postgres", "neo4j", "api", "frontend"):
            cid = self.call(f"locate_{service}", docker_compose(APP) + ["ps", "-a", "-q", service], capture=True).strip()
            if not cid or not re.fullmatch(r"[a-f0-9]{12,64}", cid):
                print(f"service={service} state=missing", flush=True)
                continue
            text = self.call(f"inspect_{service}", ["docker", "inspect", "--format", "{{json .State}}", cid], capture=True)
            state = json.loads(text)
            status = state.get("Status", "unknown")
            health = (state.get("Health") or {}).get("Status", "none")
            print(f"service={service} state={status if status in STATES else 'unknown'} "
                  f"health={health if health in HEALTH else 'unknown'} oom={bool(state.get('OOMKilled'))}", flush=True)
        print(f"disk_free_gib={shutil.disk_usage(APP).free // (1024**3)}", flush=True)

    def idle(self) -> None:
        query = "SELECT count(*) FROM stock_runtime.task_runs WHERE status IN ('queued','running','cancelling')"
        value = self.call("check_active_tasks", docker_compose(APP) + ["exec", "-T", "postgres", "psql", "-v", "ON_ERROR_STOP=1", "-At", "-U", "stock_app", "-d", "stock_daily_app", "-c", query], capture=True).strip()
        if not value.isdigit() or int(value) != 0:
            raise Refused("active_tasks_exist_do_not_restart")

    def deploy(self, sha: str, digest: str) -> None:
        archive = self.run / "source.tar.gz"
        total = 0
        check = hashlib.sha256()
        self.stage = "receive_source"
        with archive.open("wb") as handle:
            while block := sys.stdin.buffer.read(1024 * 1024):
                total += len(block)
                if total > MAX_ARCHIVE:
                    raise Refused("archive_too_large")
                check.update(block)
                handle.write(block)
        if check.hexdigest() != digest:
            raise Refused("archive_checksum_mismatch")
        source = self.run / "source"
        source.mkdir()
        unpack(archive, source)
        if shutil.disk_usage(APP).free < 3 * 1024**3:
            raise Refused("disk_free_below_3gib")
        self.idle()
        self.call("validate_compose", docker_compose(source) + ["config", "--quiet"])
        # Build while the existing containers keep running; do not delete their images.
        for service in ("api", "frontend"):
            cid = self.call(f"snapshot_{service}", docker_compose(APP) + ["ps", "-q", service], capture=True).strip()
            if not re.fullmatch(r"[a-f0-9]{12,64}", cid):
                raise Refused("existing_application_container_missing")
            image = self.call(f"save_image_{service}", ["docker", "inspect", "--format", "{{.Image}}", cid], capture=True).strip()
            (self.run / f"previous_{service}_image.txt").write_text(image + "\n")
            self.call(f"retain_image_{service}", ["docker", "image", "tag", image, f"stock_backup_{service}:{self.run.name}"])
        self.call("build_api", docker_compose(source) + ["build", "--build-arg", "TORCH_INDEX_URL=https://download.pytorch.org/whl/cpu", "api"], timeout=7200)
        self.call("build_frontend", docker_compose(source) + ["build", "frontend"], timeout=1800)
        self.idle()
        # Private backup: stdout is redirected on the HOST, not container /tmp.
        with (self.run / "database.dump").open("wb") as out:
            self.call("backup_database", docker_compose(APP) + ["exec", "-T", "postgres", "pg_dump", "-U", "stock_app", "-d", "stock_daily_app", "-Fc"], stdout=out, timeout=1800)
        with (self.run / "database.dump").open("rb") as inp:
            self.call("verify_backup", docker_compose(APP) + ["exec", "-T", "postgres", "pg_restore", "--list"], stdin=inp)
        shutil.copyfile(APP / "deploy/server.env", self.run / "server.env.backup")
        with tarfile.open(self.run / "previous_source.tar.gz", "w:gz") as backup:
            for directory, dirs, files in os.walk(APP, followlinks=False):
                base = Path(directory)
                dirs[:] = [name for name in dirs if not (base / name).is_symlink()
                           and not protected((base / name).relative_to(APP).as_posix())]
                for name in files:
                    path = base / name
                    relative = path.relative_to(APP).as_posix()
                    if not path.is_symlink() and not protected(relative):
                        backup.add(path, arcname=relative, recursive=False)
        # Preserve server.env, data, models, outputs, runtime files and all volumes.
        self.stage = "install_source"
        for path in source.rglob("*"):
            if not path.is_file():
                continue
            target = APP / path.relative_to(source)
            if target.is_symlink() or any(p.is_symlink() for p in target.parents if p != APP.parent):
                raise Refused("destination_symlink_not_allowed")
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)
        (APP / "runtime_data/users").mkdir(parents=True, exist_ok=True)
        # Only the reviewed additive auth-schema initializer; never run baseline/clean SQL.
        migration = ("from pathlib import Path; "
                     "exec('from core.config.user_credentials import _cipher; _cipher()' "
                     "if Path('core/config/user_credentials.py').is_file() else 'pass'); "
                     "from database.auth_schema import ensure_auth_schema; ensure_auth_schema(); print('schema_ok')")
        self.call("account_schema", docker_compose(APP) + ["run", "--rm", "--no-deps", "-T", "--entrypoint", "python", "api", "-c", migration], timeout=180)
        self.call("start_api", docker_compose(APP) + ["up", "-d", "--no-deps", "--no-build", "api"], timeout=180)
        self.wait_healthy("api")
        self.call("start_frontend", docker_compose(APP) + ["up", "-d", "--no-deps", "--no-build", "frontend"], timeout=180)
        self.wait_healthy("frontend")
        (APP / "DEPLOYED_REVISION").write_text(sha + "\n")
        print(f"deployment=success revision={sha}", flush=True)

    def wait_healthy(self, service: str) -> None:
        self.stage = f"health_{service}"
        cid = self.call(f"locate_new_{service}", docker_compose(APP) + ["ps", "-q", service], capture=True).strip()
        if not re.fullmatch(r"[a-f0-9]{12,64}", cid):
            raise Refused("new_container_missing")
        for _ in range(48):
            with self.log.open("ab") as log:
                result = subprocess.run(["docker", "inspect", "--format", "{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}", cid], stdout=subprocess.PIPE, stderr=log, timeout=15)
            state = result.stdout.decode().strip()
            if result.returncode == 0 and state == "healthy":
                print(f"service={service} health=healthy", flush=True)
                return
            time.sleep(5)
        raise Refused(f"{service}_healthcheck_failed")

    def diagnose(self) -> None:
        self.status()
        # Full logs stay on this server. Only fixed categories may be printed publicly.
        text = self.call("collect_api_log", docker_compose(APP) + ["logs", "--no-color", "--tail", "200", "api"], capture=True)
        patterns = {"missing_python_module": "ModuleNotFoundError", "permission_denied": "PermissionError",
                    "database_error": "OperationalError", "signal_date_mismatch": "ranking signal date mismatch",
                    "missing_user_token": "user_tushare_token_not_configured", "missing_user_llm_key": "user_llm_credential_not_configured",
                    "invalid_encryption_key": "user_credential_decryption_failed", "out_of_memory": "MemoryError",
                    "disk_full": "No space left on device", "connection_timeout": "TimeoutError"}
        found = sorted(code for code, marker in patterns.items() if marker in text)
        print("diagnostic_categories=" + (",".join(found) or "none_detected"), flush=True)
        print("raw_logs=kept_private_on_server", flush=True)


def main() -> int:
    os.umask(0o077)
    try:
        parts = parse_command(os.environ.get("SSH_ORIGINAL_COMMAND", ""))
    except Refused:
        print("error=command_not_allowed")
        return 2
    if not (APP / "deploy/server.env").is_file() or not (APP / "docker-compose.prod.yml").is_file():
        print("error=existing_deployment_not_found")
        return 2
    STATE.mkdir(parents=True, exist_ok=True)
    with (STATE / "operations.lock").open("a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("error=another_server_operation_is_running")
            return 3
        run = Path(tempfile.mkdtemp(prefix=time.strftime("run-%Y%m%d-%H%M%S-"), dir=STATE))
        ops = Ops(run)
        print(f"operation={parts[0]} record={run.name}", flush=True)
        try:
            if parts[0] == "check":
                ops.status()
                print("connection=ok", flush=True)
            elif parts[0] == "diagnose":
                ops.diagnose()
            else:
                ops.deploy(parts[1], parts[2])
            return 0
        except Exception as exc:
            # Never echo exception text: it may contain secrets or source data.
            reason = str(exc) if isinstance(exc, Refused) and re.fullmatch(r"[a-z0-9_]+", str(exc)) else type(exc).__name__
            print(f"operation=failed stage={ops.stage} reason={reason} record={run.name}", flush=True)
            print("automatic_database_restore=disabled; backups_and_private_log_retained", flush=True)
            return 1


if __name__ == "__main__":
    raise SystemExit(main())
