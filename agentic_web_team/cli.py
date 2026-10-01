from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import subprocess
import time
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen

from .config import ConfigError, default_config_path, load_config
from .logging_setup import configure_logging
from .storage import StorageError, ensure_private_dir
from .workspace import WorkspaceError


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Work with a local autonomous software engineering team")
    parser.add_argument("message", nargs="*", help="Optional first message to the team")
    parser.add_argument("--workspace", default="./project", help="Local project folder (created if missing)")
    parser.add_argument("--config", default=str(default_config_path()), help="Team and model configuration")
    parser.add_argument("--serve", action="store_true", help="Run the persistent local workflow service")
    parser.add_argument(
        "--allow-shell", action="store_true", help="Allow agents to run audited shell commands as your OS user"
    )
    parser.add_argument("--verbose", action="store_true", help="Write debug details to the service log")
    return parser


def ensure_model(base_url: str, model_name: str, state_dir: Path) -> str | None:
    if not base_url.startswith(("http://localhost:11434", "http://127.0.0.1:11434")):
        return None

    def tags() -> dict:
        with urlopen("http://127.0.0.1:11434/api/tags", timeout=2) as response:
            data = json.load(response)
            if not isinstance(data, dict):
                raise ValueError("Ollama returned an invalid model list")
            return data

    try:
        available = tags()
    except (OSError, URLError):
        if not shutil.which("ollama"):
            return "Ollama is not installed. Install Ollama, then run: ollama pull " + model_name
        ensure_private_dir(state_dir)
        with (state_dir / "ollama.log").open("a") as log:
            subprocess.Popen(["ollama", "serve"], stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        available = None
        for _ in range(20):
            time.sleep(0.5)
            try:
                available = tags()
                break
            except (OSError, URLError):
                continue
    if available is None:
        return f"Could not connect to Ollama at localhost:11434. See {state_dir / 'ollama.log'}"

    models = {item.get("name") for item in available.get("models", [])}
    if model_name not in models:
        return f"Model {model_name} is not installed. Run: ollama pull {model_name}"
    return None


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    if args.serve and args.message:
        parser.error("--serve cannot be combined with an initial chat message")
    if args.workspace.startswith("/absolute/path/to/"):
        parser.error(
            "--workspace needs a real folder. Use --workspace . for this repository, or an existing project path"
        )
    from .orchestrator import SoftwareTeam

    try:
        config = load_config(args.config)
        workspace = Path(args.workspace).expanduser().resolve()
        state_dir = workspace / config.artifact_dir
        problem = ensure_model(config.base_url, config.model_name, state_dir)
        if problem:
            parser.exit(2, problem + "\n")
        team = SoftwareTeam(config, workspace, create_workspace=True, allow_shell=args.allow_shell)
        configure_logging(state_dir, args.verbose)
        if args.serve:
            asyncio.run(team.serve())
        else:
            asyncio.run(team.chat(" ".join(args.message).strip() or None))
    except (ConfigError, StorageError, WorkspaceError, RuntimeError, OSError) as exc:
        parser.exit(2, f"agentic-web-team: {exc}\n")
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
