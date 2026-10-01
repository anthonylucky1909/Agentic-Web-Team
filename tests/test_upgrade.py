import asyncio
import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agentic_web_team.cli import main
from agentic_web_team.config import load_config
from agentic_web_team.inventory import scan_repository
from agentic_web_team.orchestrator import SoftwareTeam
from agentic_web_team.service import TeamService
from agentic_web_team.upgrade_quality import UpgradeQualityRunner
from agentic_web_team.workflow_state import WorkState
from agentic_web_team.workspace import Workspace, WorkspaceError, execute_tool, tool_schemas

CONFIG = Path(__file__).resolve().parents[1] / "config" / "team.yaml"


class UpgradeToolTests(unittest.TestCase):
    def test_cli_explains_placeholder_workspace(self):
        error = io.StringIO()
        with patch.object(sys, "argv", ["agentic-web-team", "--workspace", "/absolute/path/to/repository"]):
            with contextlib.redirect_stderr(error), self.assertRaises(SystemExit) as exit_info:
                main()
        self.assertEqual(exit_info.exception.code, 2)
        self.assertIn("Use --workspace .", error.getvalue())

    def test_inventory_and_shell_opt_in(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "package.json").write_text('{"scripts":{"test":"echo ok"},"dependencies":{"react":"1"}}')
            (root / "app.py").write_text("import json\nfrom pathlib import Path\nx = 1\n")
            (root / ".env").write_text("SECRET=hidden")
            (root / "node_modules").mkdir()
            (root / "node_modules" / "ignored.js").write_text("x")
            workspace = Workspace(root)
            report = scan_repository(workspace)
            self.assertEqual(report["file_count"], 2)
            self.assertEqual(report["manifests"][0]["dependencies"], {"react": "1"})
            self.assertIn("pathlib", report["python_imports"][0]["imports"])
            page = json.loads(execute_tool(workspace, "scan_repository", {"offset": 1, "limit": 1}, {"read"}))
            self.assertEqual(len(page["files"]), 1)
            self.assertIsNone(page["next_offset"])
            self.assertNotIn("SECRET", json.dumps(report))
            self.assertNotIn(
                "run_terminal", [tool["function"]["name"] for tool in tool_schemas({"terminal"}, {}, False)]
            )
            with self.assertRaises(WorkspaceError):
                execute_tool(workspace, "run_terminal", {"command": "echo no"}, {"terminal"})
            enabled = Workspace(root, allow_shell=True)
            result = json.loads(
                execute_tool(enabled, "run_terminal", {"command": "printf hello"}, {"terminal"}, role="fullstack")
            )
            self.assertEqual(result["output"], "hello")
            audit = (root / ".agentic_web_team" / "terminal_audit.jsonl").read_text()
            self.assertIn("printf hello", audit)
            self.assertIn("finish", audit)

    def test_broad_write_still_protects_private_paths(self):
        with tempfile.TemporaryDirectory() as temp:
            workspace = Workspace(temp)
            workspace.write_file("src/main.py", "value = 1", ["*"])
            self.assertTrue((Path(temp) / "src/main.py").is_file())
            with self.assertRaises(WorkspaceError):
                workspace.write_file(".env", "secret", ["*"])

    def test_quality_finds_python_syntax_and_gates_execution(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "pyproject.toml").write_text('[project]\nname="demo"\nversion="1"\n')
            (root / "tests").mkdir()
            (root / "tests" / "test_example.py").write_text(
                "import unittest\nclass T(unittest.TestCase):\n def test_ok(self):\n  self.assertTrue(True)\n"
            )
            checks = UpgradeQualityRunner(Workspace(root)).run()
            self.assertEqual(next(item for item in checks if item["name"] == "python_tests")["returncode"], 126)
            checks = UpgradeQualityRunner(Workspace(root, allow_shell=True)).run()
            self.assertEqual(next(item for item in checks if item["name"] == "python_tests")["returncode"], 0)

    def test_quality_detects_node_manager_and_django(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "package.json").write_text('{"scripts":{"build":"next build"}}')
            (root / "pnpm-lock.yaml").write_text("lockfileVersion: 9\n")
            (root / "backend").mkdir()
            (root / "backend" / "manage.py").write_text("print('ok')\n")
            runner = UpgradeQualityRunner(Workspace(root, allow_shell=True))
            commands = []

            def fake_command(name, argv, cwd):
                commands.append((name, argv))
                return {"name": name, "returncode": 0, "output": "ok"}

            runner._command = fake_command
            runner.run()
            self.assertIn(("root_build", ["pnpm", "run", "build"]), commands)
            self.assertTrue(any(name == "backend_django_tests" for name, _ in commands))


class UpgradeWorkflowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.team = SoftwareTeam(load_config(CONFIG), self.temp.name)
        Path(self.temp.name, "app.py").write_text("value = 1\n")

    async def _until(self, condition):
        async with asyncio.timeout(3):
            while not condition():
                await asyncio.sleep(0.01)

    async def test_upgrade_repairs_qa_finding_then_monitors(self):
        calls = []
        qa_count = 0

        async def ask(key, prompt, use_tools=True):
            nonlocal qa_count
            calls.append(key)
            if key == "qa":
                qa_count += 1
                if qa_count == 1:
                    return json.dumps(
                        {
                            "passed": False,
                            "issues": [{"owner": "fullstack", "summary": "Missing test", "evidence": "tests/"}],
                        }
                    )
                return json.dumps({"passed": True, "issues": []})
            if key == "fullstack" and "Repair these" in prompt:
                self.team.workspace.write_file("tests/test_app.py", "assert True\n", ["*"])
            return f"{key} completed"

        self.team.workflow._ask = ask
        self.team.workflow.upgrade.quality.run = lambda: [{"name": "python_tests", "returncode": 0, "output": "ok"}]
        self.team.workflow.start_upgrade("Upgrade this repository")
        await self._until(lambda: self.team.workflow.state.phase == "upgrade_monitor")
        self.assertEqual(qa_count, 2)
        self.assertEqual(self.team.workflow.state.mode, "upgrade")
        self.assertEqual(calls[:4], ["architect", "fullstack", "devops", "qa"])
        self.assertTrue(Path(self.temp.name, "tests/test_app.py").is_file())
        await self.team.workflow.stop()

    async def test_upgrade_command_and_feedback(self):
        self.team.workflow.run = lambda: asyncio.sleep(60)
        await self.team._command("/upgrade")
        self.assertIsNone(self.team.workflow.state)
        await self.team._command("/upgrade Improve the selected repository")
        self.assertEqual(self.team.workflow.state.phase, "inventory")
        self.team.workflow.state.phase = "upgrade_monitor"
        self.team.workflow.add_feedback("Improve validation")
        self.assertEqual(self.team.workflow.state.phase, "upgrade_implement")
        await self.team.workflow.stop()

    async def test_service_routes_upgrade(self):
        self.team.workflow.run = lambda: asyncio.sleep(60)
        result = await TeamService(self.team)._dispatch("upgrade", "Refactor the repository")
        self.assertIn("started", result)
        self.assertEqual(self.team.workflow.state.mode, "upgrade")
        await self.team.workflow.stop()

    async def test_custom_team_without_upgrade_roles_is_rejected(self):
        config = load_config(CONFIG)
        web_roles = {
            key: item for key, item in config.employees.items() if key not in {"architect", "fullstack", "devops"}
        }
        team = SoftwareTeam(config.model_copy(update={"employees": web_roles}), self.temp.name)
        with self.assertRaisesRegex(ValueError, "requires roles"):
            team.workflow.start_upgrade("Refactor repository")

    async def test_blocked_ignores_timing_only_check_output_changes(self):
        state = WorkState(goal="Upgrade repository", mode="upgrade", phase="blocked", status="blocked")
        state.last_signature = self.team.workspace.signature()
        state.last_checks = [{"name": "python_tests", "returncode": 1, "output": "failed in 0.10s"}]
        self.team.workflow.upgrade.quality.run = lambda: [
            {"name": "python_tests", "returncode": 1, "output": "failed in 0.11s"}
        ]
        self.team.workflow.wake_event.set()
        await self.team.workflow.upgrade.run_phase(state)
        self.assertEqual(state.phase, "blocked")
        self.team.workspace.write_file("app.py", "value = 2\n", ["*"])
        self.team.workflow.wake_event.set()
        await self.team.workflow.upgrade.run_phase(state)
        self.assertEqual(state.phase, "upgrade_verify")


if __name__ == "__main__":
    unittest.main()
