import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agentic_web_team.preview import PreviewManager, PreviewResult
from agentic_web_team.quality import QualityRunner


class PreviewTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("npm") and shutil.which("node"), "Node and npm required")
    def test_starts_and_stops_runnable_backend(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            backend = root / "backend"
            backend.mkdir()
            (backend / "node_modules").mkdir()
            (backend / "package.json").write_text(
                json.dumps(
                    {
                        "name": "preview-check",
                        "version": "1.0.0",
                        "scripts": {"dev": "node server.js"},
                    }
                )
            )
            (backend / "server.js").write_text(
                "console.log('http://127.0.0.1:' + process.env.PORT); setInterval(() => {}, 1000);\n"
            )
            preview = PreviewManager(root)
            self.addCleanup(preview.stop)
            result = preview.start()
            self.assertIn("backend: http://127.0.0.1:3001", result)
            checks = QualityRunner(root, preview).run(preview.last_results)
            self.assertEqual(checks[-1]["name"], "backend_http")
            self.assertEqual(checks[-1]["returncode"], 1)
            self.assertIn("already running", preview.start())
            first_pid = preview.processes["backend"].pid
            (backend / "server.js").write_text(
                "console.log('http://127.0.0.1:' + process.env.PORT); setInterval(() => {}, 1000); // changed\n"
            )
            self.assertIn("backend: http://127.0.0.1:3001", preview.start())
            self.assertNotEqual(first_pid, preview.processes["backend"].pid)
            self.assertIn("Stopped: backend", preview.stop())

    def test_no_project_reports_next_requirement(self):
        with tempfile.TemporaryDirectory() as temp:
            self.assertIn("No runnable", PreviewManager(Path(temp)).start())

    def test_django_requires_project_requirements(self):
        with tempfile.TemporaryDirectory() as temp:
            backend = Path(temp) / "backend"
            backend.mkdir()
            (backend / "manage.py").write_text("# placeholder")
            self.assertIn("backend/requirements.txt", PreviewManager(Path(temp)).start())

    def test_django_migrates_only_workspace_sqlite(self):
        for engine, name, should_migrate in (
            ("django.db.backends.sqlite3", "db.sqlite3", True),
            ("django.db.backends.postgresql", "production", False),
        ):
            with self.subTest(engine=engine), tempfile.TemporaryDirectory() as temp:
                backend = Path(temp) / "backend"
                backend.mkdir()
                (backend / "manage.py").write_text("# placeholder")
                (backend / "requirements.txt").write_text("Django\n")
                preview = PreviewManager(Path(temp))
                database = json.dumps({"engine": engine, "name": name})
                outputs = [(True, "check ok"), (True, database)]
                if should_migrate:
                    outputs.append((True, "migrated"))
                with (
                    patch.object(preview, "_prepare_django", return_value=None),
                    patch.object(preview, "_run", side_effect=outputs) as run,
                    patch.object(
                        preview,
                        "_launch",
                        return_value=PreviewResult("backend", "running", url="http://127.0.0.1:8000"),
                    ),
                ):
                    result = preview._start_django()
                self.assertEqual(result.status, "running")
                self.assertEqual(run.call_count, 3 if should_migrate else 2)
                self.assertEqual(bool(result.detail), not should_migrate)

    def test_malformed_manifest_and_source_signature(self):
        with tempfile.TemporaryDirectory() as temp:
            backend = Path(temp) / "backend"
            backend.mkdir()
            (backend / "package.json").write_text("[]")
            preview = PreviewManager(Path(temp))
            self.assertEqual(preview.start_results()[0].status, "failed")
            (backend / "package.json").write_text('{"scripts":{"dev":"node server.js"}}')
            (backend / "db.sqlite3").write_text("one")
            signature = preview._project_signature(backend)
            (backend / "db.sqlite3").write_text("two")
            self.assertEqual(signature, preview._project_signature(backend))
            (backend / "server.js").write_text("console.log('changed')")
            self.assertNotEqual(signature, preview._project_signature(backend))


if __name__ == "__main__":
    unittest.main()
