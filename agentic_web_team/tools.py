from __future__ import annotations

import json
from typing import Any

from .inventory import scan_repository
from .workspace import Workspace, WorkspaceError


def _schema(
    name: str, description: str, properties: dict[str, Any], required: list[str] | None = None
) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required or [],
                "additionalProperties": False,
            },
        },
    }


def tool_schemas(
    permissions: set[str], checks: dict[str, list[str]], allow_shell: bool = False
) -> list[dict[str, Any]]:
    tools: list[dict[str, Any]] = []
    string = {"type": "string"}
    if "read" in permissions:
        tools.extend(
            [
                _schema(
                    "remember_note",
                    "Save a concrete human project decision for future turns.",
                    {"note": string},
                    ["note"],
                ),
                _schema(
                    "list_files",
                    "List project files before assuming structure.",
                    {
                        "path": {"type": "string", "default": "."},
                        "max_depth": {"type": "integer", "minimum": 0, "maximum": 8, "default": 3},
                    },
                ),
                _schema(
                    "read_file",
                    "Read a project text file with line numbers.",
                    {
                        "path": string,
                        "start_line": {"type": "integer", "minimum": 1, "default": 1},
                        "end_line": {"type": "integer", "minimum": 1, "default": 400},
                    },
                    ["path"],
                ),
                _schema(
                    "search_text",
                    "Search project text for an exact substring.",
                    {
                        "query": string,
                        "path": {"type": "string", "default": "."},
                    },
                    ["query"],
                ),
                _schema("git_diff", "Show uncommitted project changes.", {}),
                _schema(
                    "scan_repository",
                    "Inventory source files, imports, manifests, scripts, and dependencies; page through all files.",
                    {
                        "offset": {"type": "integer", "minimum": 0, "maximum": 20000, "default": 0},
                        "limit": {"type": "integer", "minimum": 1, "maximum": 500, "default": 300},
                    },
                ),
            ]
        )
    if "write" in permissions:
        tools.extend(
            [
                _schema(
                    "write_file",
                    "Write a project text file inside your owned paths.",
                    {
                        "path": string,
                        "content": string,
                    },
                    ["path", "content"],
                ),
                _schema(
                    "replace_text",
                    "Replace exact text in a file inside your owned paths.",
                    {
                        "path": string,
                        "old": string,
                        "new": string,
                        "count": {"type": "integer", "minimum": 0, "default": 0},
                    },
                    ["path", "old", "new"],
                ),
            ]
        )
    if "check" in permissions and checks:
        tools.append(
            _schema(
                "run_check",
                "Run a configured verification command.",
                {
                    "name": {"type": "string", "enum": sorted(checks)},
                },
                ["name"],
            )
        )
    if "terminal" in permissions and allow_shell:
        tools.append(
            _schema(
                "run_terminal",
                "Run a shell command in the selected workspace directory. Commands are audited and time-limited.",
                {
                    "command": string,
                    "cwd": {"type": "string", "default": "."},
                    "timeout": {"type": "integer", "minimum": 1, "maximum": 300, "default": 180},
                },
                ["command"],
            )
        )
    return tools


def execute_tool(
    workspace: Workspace,
    name: str,
    args: dict[str, Any],
    permissions: set[str],
    allowed_write_paths: list[str] | None = None,
    role: str = "agent",
) -> str:
    if not isinstance(args, dict):
        raise WorkspaceError("Tool arguments must be an object")
    access = {
        "list_files": "read",
        "read_file": "read",
        "search_text": "read",
        "git_diff": "read",
        "scan_repository": "read",
        "remember_note": "read",
        "run_check": "check",
        "write_file": "write",
        "replace_text": "write",
        "run_terminal": "terminal",
    }
    needed = access.get(name)
    if needed is None:
        raise WorkspaceError(f"Unknown tool: {name}")
    if needed not in permissions:
        raise WorkspaceError(f"Permission denied for tool {name}")
    if name == "run_terminal" and not workspace.allow_shell:
        raise WorkspaceError("Terminal access is disabled; restart with --allow-shell")
    allowed_arguments = {
        "list_files": {"path", "max_depth"},
        "read_file": {"path", "start_line", "end_line"},
        "search_text": {"query", "path"},
        "git_diff": set(),
        "scan_repository": {"offset", "limit"},
        "remember_note": {"note"},
        "run_check": {"name"},
        "write_file": {"path", "content"},
        "replace_text": {"path", "old", "new", "count"},
        "run_terminal": {"command", "cwd", "timeout"},
    }
    unexpected = set(args) - allowed_arguments[name]
    if unexpected:
        raise WorkspaceError(f"Unexpected arguments for {name}: {', '.join(sorted(unexpected))}")
    try:
        if name == "list_files":
            return workspace.list_files(**args)
        if name == "read_file":
            return workspace.read_file(**args)
        if name == "search_text":
            return workspace.search_text(**args)
        if name == "write_file":
            return workspace.write_file(**args, allowed_paths=allowed_write_paths)
        if name == "replace_text":
            return workspace.replace_text(**args, allowed_paths=allowed_write_paths)
        if name == "git_diff":
            return workspace.git_diff()
        if name == "scan_repository":
            return json.dumps(scan_repository(workspace, **args), ensure_ascii=False)
        if name == "run_check":
            return workspace.run_check(**args)
        if name == "run_terminal":
            return workspace.run_terminal(**args, role=role)
        return workspace.remember_note(**args)
    except (TypeError, ValueError) as exc:
        raise WorkspaceError(f"Invalid arguments for {name}: {exc}") from exc
