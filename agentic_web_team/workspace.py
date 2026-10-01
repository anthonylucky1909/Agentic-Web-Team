from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .process import run_command
from .storage import HistoryStore, ensure_private_dir

IGNORED_DIRS = frozenset(
    {
        ".git",
        ".venv",
        "venv",
        "node_modules",
        "dist",
        "build",
        "__pycache__",
        ".agentic_web_team",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        ".tox",
        ".next",
        ".cache",
    }
)
MAX_FILE_BYTES = 2_000_000


class WorkspaceError(RuntimeError):
    pass


class Workspace:
    def __init__(
        self,
        root: str | Path,
        checks: dict[str, list[str]] | None = None,
        create: bool = False,
        allow_shell: bool = False,
    ):
        self.root = Path(root).expanduser().resolve()
        self.checks = checks or {}
        self.revision = 0
        self.allow_shell = allow_shell
        if create:
            self.root.mkdir(parents=True, exist_ok=True)
        if not self.root.is_dir():
            raise WorkspaceError(f"Workspace directory does not exist: {self.root}")

    def _resolve(self, relative: str) -> Path:
        if not isinstance(relative, str) or not relative:
            raise WorkspaceError("A workspace-relative path is required")
        candidate = (self.root / relative).resolve()
        if not candidate.is_relative_to(self.root):
            raise WorkspaceError("Path escapes the workspace")
        return candidate

    @staticmethod
    def _private_name(name: str) -> bool:
        return (
            name == ".env"
            or (name.startswith(".env.") and name != ".env.example")
            or name in {"id_rsa", "id_ed25519", "credentials.json"}
            or name.endswith((".pem", ".key"))
        )

    def _check_read_path(self, path: str) -> Path:
        resolved = self._resolve(path)
        relative = resolved.relative_to(self.root)
        if any(part in IGNORED_DIRS or self._private_name(part) for part in relative.parts):
            raise WorkspaceError(f"Private or generated path is not available to agents: {path}")
        return resolved

    def _iter_files(self, base: Path):
        if base.is_file():
            yield base
            return
        for current, dirs, files in os.walk(base, followlinks=False):
            dirs[:] = sorted(
                name for name in dirs if name not in IGNORED_DIRS and not (Path(current) / name).is_symlink()
            )
            for name in sorted(files):
                path = Path(current) / name
                if (
                    not path.is_symlink()
                    and not self._private_name(name)
                    and path.is_file()
                    and path.resolve().is_relative_to(self.root)
                ):
                    yield path

    def signature(self) -> str:
        digest = hashlib.sha256()
        for path in self._iter_files(self.root):
            stat = path.stat()
            digest.update(str(path.relative_to(self.root)).encode())
            digest.update(f":{stat.st_size}:{stat.st_mtime_ns}\n".encode())
        return digest.hexdigest()

    def list_files(self, path: str = ".", max_depth: int = 3) -> str:
        if not isinstance(max_depth, int) or not 0 <= max_depth <= 8:
            raise WorkspaceError("max_depth must be between 0 and 8")
        base = self._check_read_path(path)
        if not base.exists():
            return f"Path not found: {path}"
        if base.is_file():
            return str(base.relative_to(self.root))
        rows: list[str] = []
        base_depth = len(base.parts)
        for current, dirs, files in os.walk(base, followlinks=False):
            current_path = Path(current)
            depth = len(current_path.parts) - base_depth
            dirs[:] = sorted(
                name
                for name in dirs
                if name not in IGNORED_DIRS and depth < max_depth and not (current_path / name).is_symlink()
            )
            if depth:
                rows.append(str(current_path.relative_to(self.root)) + "/")
            rows.extend(
                str((current_path / name).relative_to(self.root))
                for name in sorted(files)
                if not self._private_name(name)
            )
            if len(rows) >= 500:
                return "\n".join(rows[:500]) + "\n... truncated ..."
        return "\n".join(rows) or "(empty)"

    def read_file(self, path: str, start_line: int = 1, end_line: int = 400) -> str:
        file_path = self._check_read_path(path)
        if not file_path.is_file():
            return f"File not found: {path}"
        if file_path.stat().st_size > MAX_FILE_BYTES:
            raise WorkspaceError(f"File exceeds {MAX_FILE_BYTES} bytes: {path}")
        if not isinstance(start_line, int) or not isinstance(end_line, int) or start_line < 1 or end_line < start_line:
            raise WorkspaceError("Invalid line range")
        lines = file_path.read_text(encoding="utf-8", errors="replace").splitlines()
        end = min(end_line, start_line + 399, len(lines))
        return "\n".join(f"{index}: {lines[index - 1]}" for index in range(start_line, end + 1))[:100_000]

    def search_text(self, query: str, path: str = ".") -> str:
        if not isinstance(query, str) or not query or len(query) > 500:
            raise WorkspaceError("Search query must be between 1 and 500 characters")
        base = self._check_read_path(path)
        matches: list[str] = []
        for index, file_path in enumerate(self._iter_files(base)):
            if index >= 5_000:
                break
            try:
                if file_path.stat().st_size > MAX_FILE_BYTES:
                    continue
                for line_number, line in enumerate(
                    file_path.read_text(encoding="utf-8", errors="ignore").splitlines(), 1
                ):
                    if query.casefold() in line.casefold():
                        matches.append(f"{file_path.relative_to(self.root)}:{line_number}: {line[:300]}")
                        if len(matches) >= 100:
                            return "\n".join(matches) + "\n... truncated ..."
            except OSError:
                continue
        return "\n".join(matches) or "No matches"

    def _check_write_path(self, path: str, allowed_paths: list[str] | None) -> Path:
        file_path = self._resolve(path)
        if file_path == self.root or any(
            part in IGNORED_DIRS or self._private_name(part) for part in file_path.relative_to(self.root).parts
        ):
            raise WorkspaceError(f"Cannot write reserved workspace path: {path}")
        if allowed_paths is not None and not any(
            prefix == "*" or file_path.is_relative_to(self._resolve(prefix)) for prefix in allowed_paths
        ):
            raise WorkspaceError(f"Write access denied for {path}; allowed: {allowed_paths}")
        return file_path

    def _write_text(self, path: Path, content: str) -> None:
        if not isinstance(content, str) or len(content.encode("utf-8")) > MAX_FILE_BYTES:
            raise WorkspaceError(f"File content must be text no larger than {MAX_FILE_BYTES} bytes")
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                "w", encoding="utf-8", dir=path.parent, prefix=".agent-write-", delete=False
            ) as stream:
                temporary = Path(stream.name)
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            if path.exists():
                os.chmod(temporary, path.stat().st_mode & 0o777)
            os.replace(temporary, path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def write_file(self, path: str, content: str, allowed_paths: list[str] | None = None) -> str:
        file_path = self._check_write_path(path, allowed_paths)
        if file_path.is_file() and file_path.read_text(encoding="utf-8", errors="replace") == content:
            return f"No changes to {path}"
        self._write_text(file_path, content)
        self.revision += 1
        return f"Wrote {path} ({len(content)} chars)"

    def replace_text(
        self, path: str, old: str, new: str, count: int = 0, allowed_paths: list[str] | None = None
    ) -> str:
        file_path = self._check_write_path(path, allowed_paths)
        if not file_path.is_file():
            return f"File not found: {path}"
        if not isinstance(old, str) or not old or not isinstance(new, str) or not isinstance(count, int) or count < 0:
            raise WorkspaceError("Invalid replacement arguments")
        if file_path.stat().st_size > MAX_FILE_BYTES:
            raise WorkspaceError("File is too large to replace")
        text = file_path.read_text(encoding="utf-8", errors="replace")
        occurrences = text.count(old)
        if occurrences == 0:
            return "Old text not found; no changes made"
        if count and occurrences < count:
            return f"Only {occurrences} occurrences found; requested {count}; no changes made"
        changed = text.replace(old, new, count or -1)
        if changed == text:
            return f"No changes to {path}"
        self._write_text(file_path, changed)
        self.revision += 1
        return f"Updated {path}; replaced {count or occurrences} occurrence(s)"

    def git_diff(self) -> str:
        try:
            result = subprocess.run(
                ["git", "diff", "--", "."], cwd=self.root, text=True, capture_output=True, timeout=30, check=False
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return f"git diff unavailable: {exc}"
        return result.stderr.strip() if result.returncode else result.stdout[-30_000:] or "No git diff"

    def run_check(self, name: str) -> str:
        command = self.checks.get(name)
        if command is None:
            return f"Unknown check '{name}'. Allowed: {sorted(self.checks)}"
        result = run_command(command, self.root, timeout=180)
        return json.dumps({"returncode": result.returncode, "output": result.output})

    def run_terminal(self, command: str, role: str, cwd: str = ".", timeout: int = 180) -> str:
        if not self.allow_shell:
            raise WorkspaceError("Terminal access is disabled; restart with --allow-shell")
        if not isinstance(command, str) or not command.strip() or len(command) > 4000:
            raise WorkspaceError("Command must be between 1 and 4,000 characters")
        if not isinstance(timeout, int) or not 1 <= timeout <= 300:
            raise WorkspaceError("Terminal timeout must be between 1 and 300 seconds")
        directory = self._check_read_path(cwd)
        if not directory.is_dir():
            raise WorkspaceError(f"Terminal working directory not found: {cwd}")
        audit = HistoryStore(self.root / ".agentic_web_team" / "terminal_audit.jsonl")
        audit.append(
            role, json.dumps({"event": "start", "at": datetime.now(UTC).isoformat(), "command": command, "cwd": cwd})
        )
        result = run_command(["/bin/sh", "-lc", command], directory, timeout=timeout)
        audit.append(
            role,
            json.dumps(
                {
                    "event": "finish",
                    "at": datetime.now(UTC).isoformat(),
                    "returncode": result.returncode,
                    "timed_out": result.timed_out,
                }
            ),
        )
        return json.dumps({"returncode": result.returncode, "output": result.output, "timed_out": result.timed_out})

    def remember_note(self, note: str) -> str:
        if not isinstance(note, str):
            raise WorkspaceError("Project note must be text")
        note = " ".join(note.strip().split())
        if not note or len(note) > 500:
            raise WorkspaceError("Project note must be between 1 and 500 characters")
        state_dir = self.root / ".agentic_web_team"
        ensure_private_dir(state_dir)
        flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(state_dir / "team_notes.md", flags, 0o600)
        try:
            os.write(fd, f"- {note}\n".encode())
        finally:
            os.close(fd)
        return "Saved project note for future team turns"


def tool_schemas(
    permissions: set[str], checks: dict[str, list[str]], allow_shell: bool = False
) -> list[dict[str, Any]]:
    from .tools import tool_schemas as schemas

    return schemas(permissions, checks, allow_shell)


def execute_tool(
    workspace: Workspace,
    name: str,
    args: dict[str, Any],
    permissions: set[str],
    allowed_write_paths: list[str] | None = None,
    role: str = "agent",
) -> str:
    from .tools import execute_tool as execute

    return execute(workspace, name, args, permissions, allowed_write_paths, role)
