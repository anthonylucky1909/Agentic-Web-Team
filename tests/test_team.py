import asyncio
import tempfile
import unittest
from pathlib import Path

from agentic_web_team.config import load_config
from agentic_web_team.orchestrator import SoftwareTeam, _is_conversation
from agentic_web_team.workspace import Workspace, WorkspaceError, execute_tool

CONFIG = Path(__file__).resolve().parents[1] / "config" / "team.yaml"


class TeamTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.team = SoftwareTeam(load_config(CONFIG), self.temp.name)

    async def test_team_developers_run_together_before_manager(self):
        release = asyncio.Event()
        started = set()
        order = []

        async def developer(key, messages, prompt, use_tools=True, fast=False):
            started.add(key)
            await release.wait()
            order.append(key)
            return f"{key} done"

        async def manager(messages, prompt, use_tools=True, fast=False):
            order.append("manager")
            self.assertIn("frontend done", prompt)
            self.assertIn("backend done", prompt)
            return "Team complete"

        self.team.agents["frontend"].chat = lambda *args, **kwargs: developer("frontend", *args, **kwargs)
        self.team.agents["backend"].chat = lambda *args, **kwargs: developer("backend", *args, **kwargs)
        self.team.agents["ux"].chat = lambda *args, **kwargs: developer("ux", *args, **kwargs)
        self.team.agents["manager"].chat = manager
        self.team.agents["qa"].chat = lambda *args, **kwargs: asyncio.sleep(0, result="QA reviewed")

        task = asyncio.create_task(self.team._team_turn("Build my app"))
        await asyncio.wait_for(self._wait_for(lambda: len(started) == 3), 1)
        self.assertEqual(started, {"ux", "frontend", "backend"})
        release.set()
        await task
        self.assertEqual(order[-1], "manager")

    async def test_addressed_messages_do_not_block_other_teammates(self):
        release = asyncio.Event()
        backend_started = asyncio.Event()

        async def frontend(*args, **kwargs):
            await release.wait()
            return "frontend done"

        async def backend(*args, **kwargs):
            backend_started.set()
            return "backend done"

        self.team.agents["frontend"].chat = frontend
        self.team.agents["backend"].chat = backend
        self.team.submit("@frontend Build the UI")
        self.team.submit("@backend Build the API")
        await asyncio.wait_for(backend_started.wait(), 1)
        self.assertFalse(release.is_set())
        release.set()
        await asyncio.gather(*self.team.pending)
        self.assertTrue(self.team.history_path.is_file())

    async def test_social_message_uses_fast_mode_without_tools(self):
        received = {}

        async def answer(messages, prompt, use_tools=True, fast=False):
            received.update(use_tools=use_tools, fast=fast, prompt=prompt)
            return "Halo!"

        self.team.agents["frontend"].chat = answer
        await self.team._answer("frontend", "Hai Faye, saya mau diskusi tampilan web.")
        self.assertEqual(received["use_tools"], False)
        self.assertEqual(received["fast"], True)
        self.assertIn("1-3 short sentences", received["prompt"])

    async def test_empty_reply_retries_without_tools(self):
        calls = []

        async def answer(messages, prompt, use_tools=True, fast=False):
            calls.append((prompt, use_tools, fast))
            return "" if len(calls) == 1 else "Frontend dan backend sudah berjalan."

        self.team.agents["manager"].chat = answer
        await self.team._answer("manager", "Bagaimana cara menjalankan project?")
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[1][1:], (False, True))
        self.assertIn("Bagaimana cara menjalankan project?", calls[1][0])
        self.assertEqual(self.team.history[-1]["message"], "Frontend dan backend sudah berjalan.")

    async def test_repeated_empty_reply_is_not_rendered_as_blank(self):
        async def answer(*args, **kwargs):
            return "   "

        self.team.agents["manager"].chat = answer
        await self.team._answer("manager", "Siapa yang online?")
        self.assertIn("Model lokal tidak mengirim jawaban", self.team.history[-1]["message"])

    async def test_cancel_stops_pending_reply(self):
        started = asyncio.Event()

        async def answer(*args, **kwargs):
            started.set()
            await asyncio.Event().wait()

        self.team.agents["frontend"].chat = answer
        self.team.submit("@frontend Hello")
        await asyncio.wait_for(started.wait(), 1)
        await self.team._command("/cancel")
        self.assertFalse(self.team.pending)

    async def test_followup_stays_with_selected_teammate(self):
        received = []

        async def frontend(messages, prompt, **kwargs):
            received.append(prompt)
            return "Faye answered"

        self.team.agents["frontend"].chat = frontend
        self.team.submit("@frontend Hai Faye")
        self.team.submit("Apa tugasmu sekarang?")
        await asyncio.gather(*self.team.pending)
        self.assertEqual(self.team.active_target, "frontend")
        self.assertEqual(len(received), 2)
        self.assertIn("The human is addressing you now:\nApa tugasmu sekarang?", received[1])
        self.assertEqual(self.team.history[-1]["speaker"], "Faye")

        self.team.submit("@team")
        self.assertEqual(self.team.active_target, "team")

    async def test_multiple_mentions_reply_in_parallel_and_keep_group(self):
        release = asyncio.Event()
        started = set()
        received = []

        async def answer(key, messages, prompt, **kwargs):
            started.add(key)
            received.append((key, prompt, kwargs))
            await release.wait()
            return f"{key} answered"

        self.team.agents["frontend"].chat = lambda *args, **kwargs: answer("frontend", *args, **kwargs)
        self.team.agents["backend"].chat = lambda *args, **kwargs: answer("backend", *args, **kwargs)
        self.team.submit('@frontend @Backend kamu sekarang masing" ngapain')
        await asyncio.wait_for(self._wait_for(lambda: len(started) == 2), 1)
        self.assertEqual(self.team.active_targets, ("frontend", "backend"))
        self.assertTrue(all(item[2]["fast"] for item in received))
        self.assertTrue(all("The human is addressing you now:\nkamu sekarang" in item[1] for item in received))
        release.set()
        await asyncio.gather(*self.team.pending)
        self.assertEqual({event["speaker"] for event in self.team.history[-2:]}, {"Faye", "Bima"})

        self.team.submit("Apa kabar kalian berdua?")
        await asyncio.gather(*self.team.pending)
        self.assertEqual(len(received), 4)

        self.team.submit("@unknown hello")
        self.assertEqual(self.team.active_targets, ("frontend", "backend"))

    async def _wait_for(self, condition):
        while not condition():
            await asyncio.sleep(0.01)


class WorkspaceTests(unittest.TestCase):
    def test_agent_write_ownership_is_enforced(self):
        with tempfile.TemporaryDirectory() as temp:
            workspace = Workspace(temp)
            with self.assertRaises(WorkspaceError):
                execute_tool(
                    workspace, "write_file", {"path": "backend/app.js", "content": "x"}, {"read", "write"}, ["frontend"]
                )
            with self.assertRaises(WorkspaceError):
                execute_tool(workspace, "write_file", {"path": "frontend/app.js", "content": "x"}, {"read"}, None)
            execute_tool(
                workspace, "write_file", {"path": "frontend/app.js", "content": "x"}, {"read", "write"}, ["frontend"]
            )
            self.assertEqual(workspace.revision, 1)

    def test_project_note_is_persistent_and_rejects_empty_note(self):
        with tempfile.TemporaryDirectory() as temp:
            workspace = Workspace(temp)
            execute_tool(workspace, "remember_note", {"note": "Use metric units"}, {"read"})
            self.assertIn("Use metric units", (Path(temp) / ".agentic_web_team" / "team_notes.md").read_text())
            with self.assertRaises(WorkspaceError):
                workspace.remember_note(" ")


class ConversationTests(unittest.TestCase):
    def test_greetings_and_role_questions_are_fast(self):
        for message in (
            "Hi Team",
            "may i know what is you task ?",
            "Hai Faye, saya mau diskusi tampilan web.",
            'kamu sekarnag masing" ngapain',
        ):
            self.assertTrue(_is_conversation(message), message)

    def test_build_request_keeps_workspace_tools(self):
        self.assertFalse(_is_conversation("Hi team, build an inventory dashboard"))
        self.assertFalse(_is_conversation("buat halaman login"))


if __name__ == "__main__":
    unittest.main()
