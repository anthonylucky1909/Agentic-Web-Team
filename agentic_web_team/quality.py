from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import urlopen

from .preview import PreviewManager, PreviewResult
from .process import run_command


class QualityRunner:
    def __init__(self, root: Path, preview: PreviewManager):
        self.root = root
        self.preview = preview

    def _check(self, name: str, command: list[str], cwd: Path) -> dict:
        result = run_command(command, cwd, timeout=180, output_limit=4000)
        return {"name": name, "returncode": result.returncode, "output": result.output}

    def run(self, previews: Sequence[PreviewResult] | None = None) -> list[dict]:
        checks: list[dict] = []
        django = self.root / "backend" / "manage.py"
        if django.is_file():
            python = self.preview.django_python
            if not python.is_file():
                checks.append(
                    {
                        "name": "backend_django_setup",
                        "returncode": 1,
                        "output": "Django environment not installed; see preview result",
                    }
                )
            else:
                for name, args in (
                    ("backend_django_check", ["check"]),
                    ("backend_migrations", ["makemigrations", "--check", "--dry-run"]),
                    ("backend_django_tests", ["test", "--noinput"]),
                ):
                    checks.append(self._check(name, [str(python), "manage.py", *args], django.parent))
        for name in ("frontend", "backend"):
            manifest = self.root / name / "package.json"
            if not manifest.is_file() or (name == "backend" and django.is_file()):
                continue
            try:
                scripts = json.loads(manifest.read_text(encoding="utf-8")).get("scripts", {})
                if not isinstance(scripts, dict):
                    raise ValueError("scripts must be an object")
            except (OSError, ValueError, TypeError) as exc:
                checks.append({"name": f"{name}_manifest", "returncode": 1, "output": str(exc)})
                continue
            for script in ("build", "test"):
                if script in scripts:
                    checks.append(self._check(f"{name}_{script}", ["npm", "run", script], manifest.parent))
        for preview in previews if previews is not None else self.preview.last_results:
            if preview.status in {"failed", "not_found"}:
                checks.append({"name": f"{preview.name}_preview", "returncode": 1, "output": preview.detail})
            if not preview.url or preview.status not in {"running", "already_running"}:
                continue
            try:
                with urlopen(preview.url, timeout=4) as response:
                    code = response.status
                checks.append(
                    {"name": f"{preview.name}_http", "returncode": 0, "output": f"{preview.url} returned HTTP {code}"}
                )
            except HTTPError as exc:
                exc.close()
                checks.append(
                    {
                        "name": f"{preview.name}_http",
                        "returncode": 1,
                        "output": f"{preview.url} returned HTTP {exc.code}",
                    }
                )
            except (OSError, URLError) as exc:
                checks.append({"name": f"{preview.name}_http", "returncode": 1, "output": f"{preview.url}: {exc}"})
        return checks
