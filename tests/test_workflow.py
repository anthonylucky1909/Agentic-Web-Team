import asyncio
import errno
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError

from agentic_web_team.agent import ToolRoundLimitError
from agentic_web_team.config import load_config
from agentic_web_team.orchestrator import SoftwareTeam
from agentic_web_team.preview import PreviewResult
from agentic_web_team.quality import QualityRunner
from agentic_web_team.service import ServiceCommandError
from agentic_web_team.workflow import WorkState
from agentic_web_team.workflow_state import Issue

CONFIG = Path(__file__).resolve().parents[1] / "config" / "team.yaml"


class WorkflowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.team = SoftwareTeam(load_config(CONFIG), self.temp.name)

    async def _until(self, condition):
        async with asyncio.timeout(2):
            while not condition():
                await asyncio.sleep(0.01)

    async def test_work_summary_shows_active_agent_and_persists_activity(self):
        started = asyncio.Event()
        release = asyncio.Event()
        self.team.workflow.state = WorkState(goal="Build a website")

        async def slow_ask(prompt, use_tools=True):
            started.set()
            await release.wait()
            return "Plan complete"

        self.team.agents["manager"].ask = slow_ask
        task = asyncio.create_task(self.team.workflow._ask("manager", "Create a plan"))
        await started.wait()
        self.assertIn("Maya since", self.team.workflow.summary())
        saved = WorkState.from_dict(json.loads(self.team.workflow.path.read_text()))
        self.assertIn("manager", saved.active_agents)
        release.set()
        self.assertEqual(await task, "Plan complete")
        self.assertEqual(self.team.workflow.state.active_agents, {})

    async def test_blocked_poll_keeps_original_phase_start(self):
        state = WorkState(
            goal="Build a website",
            status="blocked",
            phase="blocked",
            phase_started_at="2026-01-01T00:00:00+00:00",
        )
        state.last_signature = self.team.workspace.signature()
        self.team.workflow.state = state
        self.team.workflow.quality.run = lambda: []
        notices = []
        self.team.emit = notices.append
        polls = 0

        async def finish_poll():
            nonlocal polls
            polls += 1
            if polls == 2:
                self.team.workflow.stop_event.set()

        self.team.workflow._sleep_or_wake = finish_poll
        await self.team.workflow.run()
        self.assertEqual(state.phase_started_at, "2026-01-01T00:00:00+00:00")
        self.assertIn("Repair paused", self.team.workflow.summary())
        self.assertEqual(notices, ["Workflow: blocked (cycle 0)"])

    async def test_preview_http_404_fails_quality(self):
        runner = QualityRunner(self.team.workspace.root, self.team.preview)
        with patch(
            "agentic_web_team.quality.urlopen",
            side_effect=HTTPError("http://localhost:3000", 404, "Not Found", {}, None),
        ):
            checks = runner.run([PreviewResult("frontend", "running", url="http://localhost:3000")])
        self.assertEqual(checks[0]["name"], "frontend_http")
        self.assertEqual(checks[0]["returncode"], 1)

    async def test_feedback_reopens_blocked_repair(self):
        state = WorkState(goal="Build a website", status="blocked", phase="blocked")
        state.issues = [Issue("backend", "Dependency install failed", "Pillow build error", "high")]
        state.repeated_failures = 3
        state.issue_signature = "same-failure"
        self.team.workflow.state = state
        self.assertTrue(self.team.workflow.lease.acquire())
        self.team.workflow.resume = lambda: None
        try:
            self.team.workflow.add_feedback("Use a dependency set compatible with the local Python version")
            self.assertEqual(state.status, "running")
            self.assertEqual(state.phase, "repair")
            self.assertEqual(state.repeated_failures, 0)
            self.assertEqual(state.issue_signature, "")
            self.assertIn("compatible", state.feedback[-1])
        finally:
            self.team.workflow.lease.release()

    async def test_feedback_rechecks_manager_error(self):
        state = WorkState(goal="Build a website", status="blocked", phase="blocked")
        state.issues = [Issue("manager", "Workflow error", "Request timed out", "high")]
        self.team.workflow.state = state
        self.assertTrue(self.team.workflow.lease.acquire())
        self.team.workflow.resume = lambda: None
        try:
            self.team.workflow.add_feedback("Recheck the current files and tests")
            self.assertEqual(state.phase, "qa")
            self.assertEqual(state.status, "running")
        finally:
            self.team.workflow.lease.release()

    async def test_full_cycle_routes_qa_issue_and_monitors(self):
        calls = []
        qa_count = 0

        async def ask(key, prompt, use_tools=True):
            nonlocal qa_count
            calls.append((key, prompt))
            if key == "qa":
                qa_count += 1
                if qa_count == 1:
                    return json.dumps(
                        {
                            "passed": False,
                            "issues": [
                                {
                                    "owner": "frontend",
                                    "summary": "Missing submit button",
                                    "evidence": "frontend/page.html",
                                    "severity": "high",
                                }
                            ],
                        }
                    )
                return json.dumps({"passed": True, "issues": []})
            if key == "frontend" and "QA found these issues" in prompt:
                self.team.workspace.write_file("frontend/page.html", "<button>Submit</button>", ["frontend"])
            return f"{key} completed"

        self.team.workflow._ask = ask
        self.team.preview.start_results = lambda: [PreviewResult("frontend", "running", url="http://127.0.0.1:5173")]
        self.team.workflow.quality.run = lambda *args: [{"name": "frontend_build", "returncode": 0, "output": "ok"}]
        self.team.workflow.start("Build a user-specified web project")
        await self._until(lambda: self.team.workflow.state.phase == "monitor")
        self.assertEqual(qa_count, 2)
        self.assertTrue(any(key == "frontend" and "Missing submit button" in prompt for key, prompt in calls))
        self.assertEqual(self.team.workflow.state.cycle, 2)
        self.assertTrue(self.team.workflow.path.is_file())
        await self.team.workflow.stop()
        self.assertEqual(WorkState.from_dict(json.loads(self.team.workflow.path.read_text())).status, "stopped")

    async def test_feedback_wakes_monitor_and_rechecks(self):
        qa_count = 0

        async def ask(key, prompt, use_tools=True):
            nonlocal qa_count
            if key == "qa":
                qa_count += 1
                return json.dumps({"passed": True, "issues": []})
            return "done"

        self.team.workflow._ask = ask
        self.team.preview.start_results = lambda: [PreviewResult("backend", "running", url="http://127.0.0.1:8000")]
        self.team.workflow.quality.run = lambda *args: [{"name": "backend_tests", "returncode": 0, "output": "ok"}]
        self.team.workflow.start("Implement a website")
        await self._until(lambda: self.team.workflow.state.phase == "monitor")
        self.team.workflow.add_feedback("Change the signup flow")
        await self._until(lambda: qa_count == 2)
        self.assertIn("Change the signup flow", self.team.workflow.state.feedback)
        await self.team.workflow.stop()

    async def test_qa_report_gets_one_format_retry(self):
        qa_count = 0

        async def ask(key, prompt, use_tools=True):
            nonlocal qa_count
            if key == "qa":
                qa_count += 1
                return "not json" if qa_count == 1 else json.dumps({"passed": True, "issues": []})
            return "done"

        self.team.workflow._ask = ask
        self.team.preview.start_results = lambda: [PreviewResult("backend", "running", url="http://127.0.0.1:8000")]
        self.team.workflow.quality.run = lambda *args: [
            {"name": "backend_django_tests", "returncode": 0, "output": "ok"}
        ]
        self.team.workflow.start("Build a web app")
        await self._until(lambda: self.team.workflow.state.phase == "monitor")
        self.assertEqual(qa_count, 2)
        await self.team.workflow.stop()

    async def test_qa_tool_limit_retries_with_concise_checks(self):
        qa_tools = []

        async def ask(key, prompt, use_tools=True):
            if key == "qa":
                qa_tools.append(use_tools)
                if use_tools:
                    raise ToolRoundLimitError("Too many tool rounds")
                self.assertIn("backend_django_tests", prompt)
                return json.dumps({"passed": True, "issues": [], "summary": "Checks pass"})
            return "done"

        self.team.workflow._ask = ask
        self.team.preview.start_results = lambda: [PreviewResult("backend", "running", url="http://127.0.0.1:8000")]
        self.team.workflow.quality.run = lambda *args: [
            {"name": "backend_django_tests", "returncode": 0, "output": "10 tests passed"}
        ]
        self.team.workflow.start("Build a web app")
        await self._until(lambda: self.team.workflow.state.phase == "monitor")
        self.assertEqual(qa_tools, [True, False])
        await self.team.workflow.stop()

    async def test_start_command_requires_real_goal_and_stop_command_stops(self):
        await self.team._command("/start")
        self.assertIsNone(self.team.workflow.state)
        self.team.workflow.run = lambda: asyncio.sleep(60)
        await self.team._command("/start Build a booking website")
        self.assertEqual(self.team.workflow.state.goal, "Build a booking website")
        await self.team._command("/stop")
        self.assertEqual(self.team.workflow.state.status, "stopped")

    async def test_service_accepts_goal_and_stop_from_separate_chat(self):
        self.team.workflow.run = lambda: asyncio.sleep(60)
        server_task = asyncio.create_task(self.team.serve())
        try:
            await asyncio.sleep(0.05)
            if server_task.done():
                error = server_task.exception()
                if isinstance(error, OSError) and error.errno in {errno.EPERM, errno.EACCES}:
                    self.skipTest("Unix sockets are unavailable in this sandbox")
                if error:
                    raise error
            await self._until(lambda: self.team.socket_path.exists())
            client = SoftwareTeam(load_config(CONFIG), self.temp.name)
            client.service_connected = True
            with self.assertRaises(RuntimeError):
                await client.serve()
            with self.assertRaises(ServiceCommandError):
                await client.service_client.request("start", "")
            await client._command("/start Build a calendar web app")
            self.assertIn("Build a calendar web app", await client._service_request("work"))
            await client._command("/stop")
            self.assertEqual(self.team.workflow.state.status, "stopped")
        finally:
            server_task.cancel()
            await asyncio.gather(server_task, return_exceptions=True)
        self.assertFalse(self.team.socket_path.exists())

    async def test_second_team_cannot_resume_owned_workflow(self):
        self.team.workflow.run = lambda: asyncio.sleep(60)
        self.team.workflow.start("Build a project")
        other = SoftwareTeam(load_config(CONFIG), self.temp.name)
        other.workflow.resume()
        self.assertIsNone(other.workflow.task)
        with self.assertRaises(ValueError):
            other.workflow.start("Replace the active goal")
        await self.team.workflow.stop()

    async def test_external_edit_restarts_blocked_review(self):
        qa_count = 0

        async def ask(key, prompt, use_tools=True):
            nonlocal qa_count
            if key == "qa":
                qa_count += 1
                if qa_count == 1:
                    return json.dumps(
                        {
                            "passed": False,
                            "issues": [
                                {
                                    "owner": "frontend",
                                    "summary": "Missing page",
                                    "evidence": "frontend/page.html",
                                    "severity": "high",
                                }
                            ],
                        }
                    )
                return json.dumps({"passed": True, "issues": []})
            return "done"

        self.team.workflow._ask = ask
        self.team.preview.start_results = lambda: [PreviewResult("frontend", "running", url="http://127.0.0.1:5173")]
        self.team.workflow.quality.run = lambda *args: [{"name": "frontend_build", "returncode": 0, "output": "ok"}]
        self.team.workflow.start("Build a page")
        await self._until(lambda: self.team.workflow.state.phase == "blocked")
        self.team.workspace.write_file("frontend/page.html", "<main>Ready</main>", ["frontend"])
        self.team.workflow.wake_event.set()
        await self._until(lambda: self.team.workflow.state.phase == "monitor")
        self.assertEqual(qa_count, 2)
        await self.team.workflow.stop()


if __name__ == "__main__":
    unittest.main()
