from __future__ import annotations

import hashlib
import json
import os
import re
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from .process import run_command
from .storage import ensure_private_dir

URL_RE = re.compile(r"https?://(?:localhost|127\.0\.0\.1):\d+")
PreviewStatus = Literal["running", "already_running", "failed", "not_found"]


@dataclass(frozen=True)
class PreviewResult:
    name: str
    status: PreviewStatus
    detail: str = ""
    url: str | None = None
    pid: int | None = None
    log_path: Path | None = None

    def render(self) -> str:
        if self.status == "not_found":
            return self.detail
        parts = [f"{self.name}: {self.url or self.detail}"]
        if self.status == "already_running":
            parts.append("already running")
        if self.pid is not None:
            parts.append(f"PID {self.pid}")
        if self.log_path is not None:
            parts.append(f"log: {self.log_path}")
        if self.url and self.detail:
            parts.append(self.detail)
        return parts[0] + (f" ({'; '.join(parts[1:])})" if len(parts) > 1 else "")


class PreviewManager:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.state_dir = self.root / ".agentic_web_team"
        self.processes: dict[str, subprocess.Popen[str]] = {}
        self.urls: dict[str, str] = {}
        self.signatures: dict[str, str] = {}
        self.last_results: list[PreviewResult] = []

    @property
    def django_python(self) -> Path:
        return self.state_dir / "django_venv" / "bin" / "python"

    def _run(self, command: list[str], cwd: Path, timeout: int) -> tuple[bool, str]:
        result = run_command(command, cwd, timeout, output_limit=4000)
        return result.ok, result.output

    def _prepare_django(self) -> str | None:
        backend = self.root / "backend"
        requirements = backend / "requirements.txt"
        if not requirements.is_file():
            return "Django needs backend/requirements.txt"
        if not self.django_python.is_file():
            success, output = self._run(
                [sys.executable, "-m", "venv", str(self.django_python.parent.parent)], backend, 90
            )
            if not success:
                return f"Virtual environment failed: {output}"
        digest = hashlib.sha256(requirements.read_bytes()).hexdigest()
        marker = self.state_dir / "django_requirements.sha256"
        if not marker.is_file() or marker.read_text() != digest:
            success, output = self._run(
                [str(self.django_python), "-m", "pip", "install", "-r", str(requirements)], backend, 300
            )
            if not success:
                return f"Dependency install failed: {output}"
            marker.write_text(digest)
        return None

    def _project_signature(self, directory: Path) -> str:
        digest = hashlib.sha256()
        for current, dirs, files in os.walk(directory, followlinks=False):
            dirs[:] = sorted(
                name
                for name in dirs
                if name not in {"node_modules", ".venv", "venv", "__pycache__", "dist", "build", ".git"}
                and not (Path(current) / name).is_symlink()
            )
            for name in sorted(files):
                if name.endswith((".sqlite3", ".db", ".pyc")):
                    continue
                path = Path(current) / name
                if path.is_symlink():
                    continue
                info = path.stat()
                digest.update(str(path.relative_to(directory)).encode())
                digest.update(f":{info.st_size}:{info.st_mtime_ns}\n".encode())
        return digest.hexdigest()

    def _existing(self, name: str, signature: str) -> PreviewResult | None:
        process = self.processes.get(name)
        if process and process.poll() is None:
            if self.signatures.get(name) != signature:
                self._stop_one(name)
                return None
            return PreviewResult(name, "already_running", url=self.urls.get(name), pid=process.pid)
        return None

    def _launch(self, name: str, command: list[str], cwd: Path, env: dict[str, str], default_url: str) -> PreviewResult:
        log_path = self.state_dir / f"{name}.log"
        try:
            with log_path.open("w", encoding="utf-8") as log:
                process = subprocess.Popen(
                    command, cwd=cwd, env=env, stdout=log, stderr=subprocess.STDOUT, start_new_session=True, text=True
                )
        except OSError as exc:
            return PreviewResult(name, "failed", f"Could not start: {exc}")
        self.processes[name] = process
        deadline = time.monotonic() + 8
        log_text = ""
        while time.monotonic() < deadline:
            time.sleep(0.2)
            with log_path.open("rb") as log:
                log.seek(0, os.SEEK_END)
                log.seek(max(0, log.tell() - 4000))
                log_text = log.read().decode("utf-8", errors="replace")
            if process.poll() is not None or URL_RE.search(log_text):
                break
        if process.poll() is not None:
            self.processes.pop(name, None)
            self.urls.pop(name, None)
            return PreviewResult(
                name, "failed", f"Exited ({process.returncode}): {log_text[-1000:]}", log_path=log_path
            )
        match = URL_RE.search(log_text)
        url = match.group(0) if match else default_url
        self.urls[name] = url
        return PreviewResult(name, "running", url=url, pid=process.pid, log_path=log_path)

    def _start_django(self) -> PreviewResult:
        backend = self.root / "backend"
        signature = self._project_signature(backend)
        existing = self._existing("backend", signature)
        if existing:
            return existing
        try:
            problem = self._prepare_django()
            if problem:
                return PreviewResult("backend", "failed", problem)
            success, output = self._run([str(self.django_python), "manage.py", "check"], backend, 90)
            if not success:
                return PreviewResult("backend", "failed", f"Django check failed: {output}")
            success, output = self._run(
                [
                    str(self.django_python),
                    "manage.py",
                    "shell",
                    "-c",
                    "from django.conf import settings; import json; "
                    "print(json.dumps({'engine': settings.DATABASES['default']['ENGINE'], "
                    "'name': str(settings.DATABASES['default']['NAME'])}))",
                ],
                backend,
                45,
            )
            if not success:
                return PreviewResult("backend", "failed", f"Database configuration failed: {output}")
            settings_line = next(
                line for line in reversed(output.splitlines()) if line.startswith("{") and "engine" in line
            )
            settings = json.loads(settings_line)
            sqlite_path = (backend / settings["name"]).resolve()
            database_note = ""
            if settings["engine"] == "django.db.backends.sqlite3" and sqlite_path.is_relative_to(self.root):
                success, output = self._run(
                    [str(self.django_python), "manage.py", "migrate", "--noinput"], backend, 120
                )
                if not success:
                    return PreviewResult("backend", "failed", f"Migration failed: {output}")
            else:
                database_note = "External database migration was not run automatically"
            env = os.environ.copy()
            env["PYTHONUNBUFFERED"] = "1"
            result = self._launch(
                "backend",
                [str(self.django_python), "manage.py", "runserver", "127.0.0.1:8000", "--noreload"],
                backend,
                env,
                "http://127.0.0.1:8000",
            )
            if result.status == "running":
                self.signatures["backend"] = signature
            if result.status == "running" and database_note:
                return PreviewResult(result.name, result.status, database_note, result.url, result.pid, result.log_path)
            return result
        except (OSError, ValueError, KeyError, IndexError, StopIteration) as exc:
            return PreviewResult("backend", "failed", f"Django preview failed: {exc}")

    def _start_node(self, name: str, directory: Path, script: str) -> PreviewResult:
        signature = self._project_signature(directory)
        existing = self._existing(name, signature)
        if existing:
            return existing
        manifest = directory / "package.json"
        lockfile = directory / "package-lock.json"
        digest = hashlib.sha256(
            manifest.read_bytes() + (lockfile.read_bytes() if lockfile.is_file() else b"")
        ).hexdigest()
        marker = self.state_dir / f"{name}_dependencies.sha256"
        needs_install = not (directory / "node_modules").is_dir() or (marker.is_file() and marker.read_text() != digest)
        if needs_install:
            command = ["npm", "ci"] if lockfile.is_file() else ["npm", "install"]
            success, output = self._run(command, directory, 300)
            if not success:
                return PreviewResult(name, "failed", f"Dependency install failed: {output}")
        marker.write_text(digest)
        env = os.environ.copy()
        env["HOST"] = "127.0.0.1"
        env["PORT"] = "3001" if name == "backend" else "5173"
        result = self._launch(name, ["npm", "run", script], directory, env, f"http://127.0.0.1:{env['PORT']}")
        if result.status == "running":
            self.signatures[name] = self._project_signature(directory)
        return result

    def start_results(self) -> list[PreviewResult]:
        ensure_private_dir(self.state_dir)
        results: list[PreviewResult] = []
        django = (self.root / "backend" / "manage.py").is_file()
        if django:
            results.append(self._start_django())
        for name in ("backend", "frontend"):
            if name == "backend" and django:
                continue
            directory = self.root / name
            manifest = directory / "package.json"
            if not manifest.is_file():
                continue
            try:
                data = json.loads(manifest.read_text(encoding="utf-8"))
                if not isinstance(data, dict):
                    raise ValueError("package.json must be an object")
                scripts = data.get("scripts", {})
                if not isinstance(scripts, dict):
                    raise ValueError("scripts must be an object")
                script = "dev" if "dev" in scripts else "start" if "start" in scripts else None
                if script:
                    results.append(self._start_node(name, directory, script))
            except (OSError, ValueError, TypeError) as exc:
                results.append(PreviewResult(name, "failed", f"Invalid package.json or dependencies: {exc}"))
        if not results:
            results.append(
                PreviewResult("workspace", "not_found", "No runnable Django backend or Node dev/start script yet.")
            )
        self.last_results = results
        return results

    def start(self) -> str:
        return "\n".join(result.render() for result in self.start_results())

    def _stop_one(self, name: str) -> bool:
        process = self.processes.pop(name, None)
        self.urls.pop(name, None)
        self.signatures.pop(name, None)
        if process is None or process.poll() is not None:
            return False
        try:
            os.killpg(process.pid, signal.SIGTERM)
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait(timeout=5)
        except ProcessLookupError:
            pass
        return True

    def stop(self) -> str:
        stopped = [name for name in list(self.processes) if self._stop_one(name)]
        self.last_results = []
        return f"Stopped: {', '.join(stopped)}" if stopped else "No preview started in this session."
