from __future__ import annotations

import ast
import json
import os
import shutil
import sys
import tomllib
from pathlib import Path
from typing import Any

from .process import run_command
from .workspace import MAX_FILE_BYTES, Workspace


class UpgradeQualityRunner:
    def __init__(self, workspace: Workspace):
        self.workspace = workspace
        self.root = workspace.root

    def _command(self, name: str, argv: list[str], cwd: Path) -> dict[str, Any]:
        if not self.workspace.allow_shell:
            return {"name": name, "returncode": 126, "output": "Execution disabled; restart with --allow-shell"}
        result = run_command(argv, cwd, timeout=180, env={**os.environ, "CI": "true"}, output_limit=6000)
        return {"name": name, "returncode": result.returncode, "output": result.output}

    def _uses_pytest(self, tools: dict[str, Any], dependencies: list[str]) -> bool:
        if "pytest" in tools or any("pytest" in dependency.lower() for dependency in dependencies):
            return True
        for path in (*self.root.glob("test_*.py"), *(self.root / "tests").glob("test_*.py")):
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, SyntaxError):
                continue
            for node in tree.body:
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test_"):
                    return True
                if isinstance(node, ast.Import) and any(alias.name == "pytest" for alias in node.names):
                    return True
                if isinstance(node, ast.ImportFrom) and node.module == "pytest":
                    return True
                if isinstance(node, ast.ClassDef) and node.name.startswith("Test"):
                    if not any(
                        isinstance(base, ast.Attribute)
                        and base.attr == "TestCase"
                        or isinstance(base, ast.Name)
                        and base.id == "TestCase"
                        for base in node.bases
                    ):
                        return True
        return False

    def run(self) -> list[dict[str, Any]]:
        checks: list[dict[str, Any]] = []
        python_files = [path for path in self.workspace._iter_files(self.root) if path.suffix == ".py"]
        if python_files:
            syntax_errors = []
            for path in python_files[:5000]:
                try:
                    if path.stat().st_size <= MAX_FILE_BYTES:
                        ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
                except (OSError, UnicodeError, SyntaxError) as exc:
                    syntax_errors.append(f"{path.relative_to(self.root)}: {exc}")
            checks.append(
                {
                    "name": "python_syntax",
                    "returncode": 1 if syntax_errors else 0,
                    "output": "\n".join(syntax_errors[:30]) or f"Parsed {min(len(python_files), 5000)} Python files",
                }
            )
            if len(python_files) > 5000:
                checks.append(
                    {"name": "python_syntax_coverage", "returncode": 1, "output": "More than 5,000 Python files"}
                )
        pyproject = self.root / "pyproject.toml"
        if pyproject.is_file():
            try:
                config = tomllib.loads(pyproject.read_text(encoding="utf-8"))
                tools = config.get("tool", {})
                if not isinstance(tools, dict):
                    raise ValueError("tool must be a table")
                checks.append({"name": "pyproject_parse", "returncode": 0, "output": "Valid TOML"})
                python = self.root / ".venv" / "bin" / "python"
                python_exe = str(python) if python.is_file() else sys.executable
                if (self.root / "tests").is_dir() or "pytest" in tools or list(self.root.glob("test_*.py")):
                    dependencies = config.get("project", {}).get("dependencies", [])
                    use_pytest = self._uses_pytest(tools, dependencies)
                    argv = (
                        [python_exe, "-m", "pytest", "-q"]
                        if use_pytest
                        else [python_exe, "-m", "unittest", "discover", "-s", "tests"]
                    )
                    checks.append(self._command("python_tests", argv, self.root))
                if "ruff" in tools:
                    ruff = self.root / ".venv" / "bin" / "ruff"
                    executable = str(ruff) if ruff.is_file() else shutil.which("ruff")
                    checks.append(
                        self._command("python_lint", [executable, "check", "."], self.root)
                        if executable
                        else {"name": "python_lint", "returncode": 127, "output": "ruff is not installed"}
                    )
                if "mypy" in tools:
                    mypy = self.root / ".venv" / "bin" / "mypy"
                    executable = str(mypy) if mypy.is_file() else shutil.which("mypy")
                    checks.append(
                        self._command("python_types", [executable, "."], self.root)
                        if executable
                        else {"name": "python_types", "returncode": 127, "output": "mypy is not installed"}
                    )
            except (OSError, UnicodeError, ValueError) as exc:
                checks.append({"name": "pyproject_parse", "returncode": 1, "output": str(exc)})
        elif (self.root / "tests").is_dir() and python_files:
            checks.append(
                self._command("python_tests", [sys.executable, "-m", "unittest", "discover", "-s", "tests"], self.root)
            )
        manage_files = [path for path in self.workspace._iter_files(self.root) if path.name == "manage.py"][:10]
        for manage in manage_files:
            label = manage.parent.relative_to(self.root).as_posix().replace("/", "_") or "root"
            python = manage.parent / ".venv" / "bin" / "python"
            python_exe = str(python) if python.is_file() else sys.executable
            checks.append(self._command(f"{label}_django_check", [python_exe, "manage.py", "check"], manage.parent))
            checks.append(
                self._command(f"{label}_django_tests", [python_exe, "manage.py", "test", "--noinput"], manage.parent)
            )
        manifests = [path for path in self.workspace._iter_files(self.root) if path.name == "package.json"][:30]
        for manifest in manifests:
            label = manifest.parent.relative_to(self.root).as_posix().replace("/", "_")
            label = "root" if label == "." else label
            try:
                data = json.loads(manifest.read_text(encoding="utf-8"))
                scripts = data.get("scripts", {})
                if not isinstance(scripts, dict):
                    raise ValueError("scripts must be an object")
                checks.append({"name": f"{label}_manifest", "returncode": 0, "output": "Valid JSON"})
                runner = (
                    "pnpm"
                    if (manifest.parent / "pnpm-lock.yaml").is_file()
                    else "yarn"
                    if (manifest.parent / "yarn.lock").is_file()
                    else "npm"
                )
                for script in ("lint", "typecheck", "test", "build"):
                    if script in scripts:
                        checks.append(self._command(f"{label}_{script}", [runner, "run", script], manifest.parent))
            except (OSError, UnicodeError, ValueError, TypeError) as exc:
                checks.append({"name": f"{label}_manifest", "returncode": 1, "output": str(exc)})
        if (self.root / "go.mod").is_file():
            checks.append(self._command("go_tests", ["go", "test", "./..."], self.root))
        if (self.root / "Cargo.toml").is_file():
            checks.append(self._command("cargo_tests", ["cargo", "test", "--locked"], self.root))
        return checks
