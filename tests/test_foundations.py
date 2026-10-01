import json
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

from agentic_web_team.config import ConfigError, load_config, load_skill_text
from agentic_web_team.process import run_command
from agentic_web_team.storage import HistoryStore, JsonStateStore, StorageError, WorkflowLease
from agentic_web_team.workspace import Workspace, WorkspaceError, execute_tool

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "config" / "team.yaml"


class ConfigTests(unittest.TestCase):
    def test_checkout_and_packaged_defaults_match(self):
        packaged = ROOT / "agentic_web_team" / "defaults" / "team.yaml"
        self.assertEqual(yaml.safe_load(CONFIG.read_text()), yaml.safe_load(packaged.read_text()))
        loaded = load_config(packaged)
        self.assertIn("Senior Backend", load_skill_text(loaded, ["backend"]))
        for source in (ROOT / "skills").glob("*/SKILL.md"):
            bundled = ROOT / "agentic_web_team" / "defaults" / "skills" / source.parent.name / "SKILL.md"
            self.assertEqual(source.read_text(), bundled.read_text())

    def test_environment_override_and_url_validation(self):
        config = load_config(
            CONFIG, env={"AGENTIC_WEB_TEAM_MODEL": "other:latest", "AGENTIC_WEB_TEAM_MONITOR_SECONDS": "45"}
        )
        self.assertEqual(config.model_name, "other:latest")
        self.assertEqual(config.monitor_interval_seconds, 45)
        with self.assertRaises(ConfigError):
            load_config(CONFIG, env={"AGENTIC_WEB_TEAM_BASE_URL": "http://example.com/v1"})

    def test_rejects_unsafe_role_path_and_missing_skill(self):
        with tempfile.TemporaryDirectory() as temp:
            config_path = Path(temp) / "team.yaml"
            data = yaml.safe_load(CONFIG.read_text())
            data["employees"]["frontend"]["write_paths"] = ["../outside"]
            config_path.write_text(yaml.safe_dump(data))
            with self.assertRaises(ConfigError):
                load_config(config_path)
        with self.assertRaises(ConfigError):
            load_skill_text(load_config(CONFIG), ["missing_skill"])


class StorageTests(unittest.TestCase):
    def test_state_roundtrip_and_corrupt_state_is_reported(self):
        with tempfile.TemporaryDirectory() as temp:
            store = JsonStateStore(Path(temp) / "state" / "work.json")
            store.save({"goal": "Build a project"})
            self.assertEqual(store.load(), {"goal": "Build a project"})
            store.path.write_text("not json")
            with self.assertRaises(StorageError):
                store.load()

    def test_history_is_shared_and_private_symlinks_are_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "state" / "history.jsonl"
            HistoryStore(path).append("You", "First")
            HistoryStore(path).append("Faye", "Second")
            self.assertEqual([item["speaker"] for item in HistoryStore(path).recent()], ["You", "Faye"])
            outside = Path(temp) / "outside.json"
            outside.write_text(json.dumps({"goal": "secret"}))
            link = Path(temp) / "state" / "linked.json"
            link.symlink_to(outside)
            with self.assertRaises(StorageError):
                JsonStateStore(link).load()

    def test_only_one_lease_can_own_workspace(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "state" / "workflow.lock"
            first = WorkflowLease(path)
            second = WorkflowLease(path)
            self.assertTrue(first.acquire())
            self.assertFalse(second.acquire())
            first.release()
            self.assertTrue(second.acquire())
            second.release()


class WorkspaceSecurityTests(unittest.TestCase):
    def test_private_files_and_external_symlinks_are_not_readable(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "project"
            root.mkdir()
            (root / ".env").write_text("SECRET_TOKEN=private")
            outside = Path(temp) / "outside.txt"
            outside.write_text("private marker")
            (root / "outside-link.txt").symlink_to(outside)
            workspace = Workspace(root)
            self.assertNotIn("SECRET_TOKEN", workspace.search_text("SECRET_TOKEN"))
            self.assertNotIn("private marker", workspace.search_text("private marker"))
            with self.assertRaises(WorkspaceError):
                workspace.read_file("outside-link.txt")
            with self.assertRaises(WorkspaceError):
                workspace.read_file(".env")

    def test_tool_arguments_and_empty_replacements_are_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            workspace = Workspace(temp)
            workspace.write_file("frontend/page.txt", "hello", ["frontend"])
            with self.assertRaises(WorkspaceError):
                execute_tool(
                    workspace,
                    "write_file",
                    {"path": "frontend/page.txt", "content": "x", "allowed_paths": None},
                    {"write"},
                    ["frontend"],
                )
            with self.assertRaises(WorkspaceError):
                workspace.replace_text("frontend/page.txt", "", "x", allowed_paths=["frontend"])


class ProcessTests(unittest.TestCase):
    def test_timeout_returns_bounded_result(self):
        with tempfile.TemporaryDirectory() as temp:
            result = run_command([sys.executable, "-c", "import time; time.sleep(5)"], Path(temp), timeout=1)
            self.assertEqual(result.returncode, 124)
            self.assertTrue(result.timed_out)


if __name__ == "__main__":
    unittest.main()
