from __future__ import annotations

import ast
import json
import tomllib
from collections import Counter
from pathlib import Path
from typing import Any

from .workspace import MAX_FILE_BYTES, Workspace

MANIFESTS = {
    "pyproject.toml",
    "requirements.txt",
    "Pipfile",
    "poetry.lock",
    "uv.lock",
    "package.json",
    "package-lock.json",
    "pnpm-lock.yaml",
    "yarn.lock",
    "Cargo.toml",
    "go.mod",
    "Gemfile",
    "Dockerfile",
    "compose.yaml",
}


def scan_repository(workspace: Workspace, max_files: int = 20_000, offset: int = 0, limit: int = 300) -> dict[str, Any]:
    if not 1 <= max_files <= 20_000:
        raise ValueError("max_files must be between 1 and 20,000")
    if not 0 <= offset <= 20_000 or not 1 <= limit <= 500:
        raise ValueError("offset must be 0..20,000 and limit must be 1..500")
    paths: list[str] = []
    extensions: Counter[str] = Counter()
    manifests: list[dict[str, Any]] = []
    python_imports: list[dict[str, Any]] = []
    truncated = False
    for index, path in enumerate(workspace._iter_files(workspace.root)):
        if index >= max_files:
            truncated = True
            break
        try:
            size = path.stat().st_size
        except OSError:
            continue
        relative = path.relative_to(workspace.root).as_posix()
        paths.append(relative)
        extensions[path.suffix or "(none)"] += 1
        if path.name in MANIFESTS and len(manifests) < 100:
            manifests.append(_manifest(path, relative))
        if path.suffix == ".py" and len(python_imports) < 1000 and size <= MAX_FILE_BYTES:
            imports = _python_imports(path)
            if imports:
                python_imports.append({"path": relative, "imports": imports[:50]})
    return {
        "file_count": len(paths),
        "truncated": truncated,
        "extensions": dict(extensions.most_common(30)),
        "manifests": manifests[:30],
        "python_imports": python_imports[:100],
        "files": paths[offset : offset + limit],
        "next_offset": offset + limit if offset + limit < len(paths) else None,
        "file_list_truncated": offset + limit < len(paths),
    }


def _python_imports(path: Path) -> list[str]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, SyntaxError):
        return []
    imports: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imports.add("." * node.level + (node.module or ""))
    return sorted(imports)


def _manifest(path: Path, relative: str) -> dict[str, Any]:
    entry: dict[str, Any] = {"path": relative}
    try:
        if path.stat().st_size > MAX_FILE_BYTES:
            entry["note"] = "too large to inspect"
            return entry
        if path.name == "package.json":
            data = json.loads(path.read_text(encoding="utf-8"))
            dependencies = data.get("dependencies", {})
            dev_dependencies = data.get("devDependencies", {})
            if isinstance(dependencies, dict):
                entry["dependencies"] = dict(sorted(dependencies.items())[:100])
            if isinstance(dev_dependencies, dict):
                entry["dev_dependencies"] = dict(sorted(dev_dependencies.items())[:100])
            scripts = data.get("scripts", {})
            if isinstance(scripts, dict):
                entry["scripts"] = {str(key): str(value)[:200] for key, value in list(scripts.items())[:30]}
        elif path.name == "pyproject.toml":
            data = tomllib.loads(path.read_text(encoding="utf-8"))
            entry["dependencies"] = data.get("project", {}).get("dependencies", [])[:100]
            entry["tools"] = sorted(data.get("tool", {}))
            entry["build_backend"] = data.get("build-system", {}).get("build-backend")
        elif path.name == "requirements.txt":
            entry["requirements"] = [
                line.strip()
                for line in path.read_text(encoding="utf-8").splitlines()
                if line.strip() and not line.lstrip().startswith("#")
            ][:100]
    except (OSError, UnicodeError, ValueError, TypeError, AttributeError) as exc:
        entry["error"] = f"{type(exc).__name__}: {exc}"
    return entry
