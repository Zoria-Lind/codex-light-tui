"""Regression coverage for independently running chats in one TUI."""

import asyncio
from pathlib import Path
import subprocess
import sys
from unittest.mock import patch

from textual.widgets import Input, TextArea

from dialogs import Approval, ProjectDialog, Questions
from main import CodexLight


class TestApp(CodexLight):
    CSS_PATH = Path(__file__).with_name("app.tcss")

    def connect(self):
        pass

    def sync_local(self):
        pass


class TestBackend:
    shared_url = "test"
    shared_mode = True

    def __init__(self, app, rows):
        self.app = app
        self.rows = rows
        self.sent = []
        self.requests = []
        self.active = set()
        self.wait_for_history = None
        self.history_started = asyncio.Event()

    async def request(self, method, params=None):
        self.requests.append((method, params))
        if method == "thread/list":
            return {"data": list(self.rows.values())}
        if method == "thread/turns/list" or method == "project/list":
            return {"data": []}
        if method == "project/create":
            return {"project": {"id": "new-project-id", **params}}
        if method == "thread/start":
            thread = {"id": "project-thread", "cwd": params["cwd"], "projectId": params.get("projectId"), "updatedAt": 1}
            self.rows[thread["id"]] = thread
            return {"thread": thread}
        tid = params["threadId"]
        if method == "thread/read" and tid == "a" and self.wait_for_history is not None:
            self.history_started.set()
            await self.wait_for_history.wait()
        if method in {"thread/read", "thread/resume"}:
            return {"thread": {**self.rows[tid], "status": {"type": "inactive"}}}
        if method == "turn/start":
            self.active.add(tid)
            return {"turn": {"id": f"turn-{tid}"}}
        if method == "turn/interrupt":
            self.active.remove(tid)
            await self.app.backend_event({
                "method": "turn/completed",
                "params": {"threadId": tid, "turn": {"id": params["turnId"], "status": "interrupted"}},
            })
            return {}
        if method == "thread/unsubscribe":
            assert tid not in self.active
            return {}
        raise AssertionError(method)

    async def close(self):
        pass

    async def send(self, message):
        self.sent.append(message)


async def run():
    with patch("main.read_state", return_value={}):
        app = TestApp()
    rows = {tid: {"id": tid, "name": tid, "cwd": str(Path.cwd()), "updatedAt": 1} for tid in ("a", "b")}
    backend = app.backend = TestBackend(app, rows)
    with patch("main.desktop_preferences", return_value=({}, {}, {}, [], {})):
        await exercise(app, backend, rows)


async def wait_for_dialog(app, selector):
    async with asyncio.timeout(3):
        while not (control := next(iter(app.screen.query(selector)), None)) or not control.region:
            await asyncio.sleep(0.01)
    return next(worker for worker in app.workers if worker.group == "approval")


async def exercise(app, backend, rows):
    async with app.run_test(size=(118, 40)) as pilot:
        app.rows = list(rows.values())
        app.connected = True
        prompt = app.query_one("#prompt", TextArea)
        await app.open_thread("a").wait()
        a = app.chat
        prompt.load_text("first")
        a.attachments.append("a.png")
        await app.action_send_prompt().wait()
        prompt.load_text("unsent A")
        a.attachments.append("draft-a.png")
        await app.open_thread("b").wait()
        b = app.chat
        prompt.load_text("second")
        b.attachments.append("b.png")
        await app.action_send_prompt().wait()
        assert backend.active == {"a", "b"} and app.subscriptions == {"a", "b"}

        await app.backend_event({"method": "item/completed", "params": {
            "threadId": "a", "item": {"id": "a-output", "type": "agentMessage", "text": "answer A"},
        }})
        questions = [
            {"id": "choice", "question": "Choose", "options": [{"label": "One"}], "isOther": True},
            {"id": "text", "question": "Explain"},
        ]
        await app.backend_event({"id": 87, "method": "item/tool/requestUserInput", "params": {
            "threadId": "a", "turnId": "turn-a", "questions": questions,
        }})
        await pilot.pause()
        assert app.chat is b and not isinstance(app.screen, Questions) and not backend.sent
        assert a.messages == [("Codex", "answer A")] and not b.messages
        assert 87 in a.requests
        await app.open_thread("a").wait()
        await pilot.pause()
        assert app.chat is a and prompt.text == "unsent A" and a.attachments == ["draft-a.png"]
        response = await wait_for_dialog(app, "#choice-0-other")
        assert isinstance(app.screen, Questions)
        await pilot.click("#choice-0-other")
        app.screen.query_one("#answer-0", Input).value = "custom"
        await pilot.press("enter")
        app.screen.query_one("#answer-1", Input).value = "reason"
        await pilot.click("#question-back")
        await pilot.click("#choice-0-0")
        await pilot.press("enter")
        await pilot.press("enter")
        await asyncio.wait_for(response.wait(), 3)
        assert backend.sent == [{"id": 87, "result": {"answers": {
            "choice": {"answers": ["One"]}, "text": {"answers": ["reason"]},
        }}}]
        assert not a.requests
        await app.backend_event({"id": 88, "method": "item/tool/requestUserInput", "params": {
            "threadId": "a", "turnId": "turn-a", "questions": [questions[0]],
        }})
        response = await wait_for_dialog(app, "#choice-0-other")
        await pilot.click("#choice-0-other")
        app.screen.query_one("#answer-0", Input).value = "custom answer"
        await pilot.press("enter")
        await asyncio.wait_for(response.wait(), 3)
        assert backend.sent[-1] == {"id": 88, "result": {"answers": {"choice": {"answers": ["custom answer"]}}}}
        await app.backend_event({"id": 89, "method": "item/permissions/requestApproval", "params": {
            "threadId": "b", "turnId": "turn-b", "permissions": {"network": {"enabled": True}},
        }})
        await pilot.pause()
        assert 89 in b.requests and len(backend.sent) == 2
        await app.stop_turn(a).wait()
        assert backend.active == {"b"} and b.active_turn == "turn-b"
        await app.open_thread("b").wait()
        response = await wait_for_dialog(app, "#decline")
        assert isinstance(app.screen, Approval)
        await pilot.press("escape")
        await asyncio.wait_for(response.wait(), 3)
        assert 89 in b.requests and len(backend.sent) == 2
        await pilot.click("#pending-request")
        response = await wait_for_dialog(app, "#decline")
        await pilot.click("#decline")
        await asyncio.wait_for(response.wait(), 3)
        assert backend.sent[-1] == {"id": 89, "result": {"permissions": {}, "scope": "turn"}}
        await app.stop_turn(b).wait()
        assert not backend.active
        starts = [params for method, params in backend.requests if method == "turn/start"]
        assert [(p["threadId"], p["input"][1]["path"]) for p in starts] == [("a", "a.png"), ("b", "b.png")]

        backend.wait_for_history = asyncio.Event()
        selection = app.open_thread("a")
        await asyncio.wait_for(backend.history_started.wait(), 2)
        assert app.chat is a and prompt.text == "unsent A"
        assert len(app.query_one("#history").children) == 1
        backend.wait_for_history.set()
        await selection.wait()

        creation = app.create_project()
        await pilot.pause()
        assert isinstance(app.screen, ProjectDialog)
        app.screen.query_one("#project-name", Input).value = "Example project"
        await pilot.click("#create-project")
        await creation.wait()
        assert app.chat.project_id == "new-project-id" and app.chat.current["id"] is None
        prompt.load_text("new project chat")
        await app.action_send_prompt().wait()
        assert app.chat.current["projectId"] == "new-project-id"
        await app.stop_turn().wait()
        await pilot.pause()
    print("PASS: parallel send, draft isolation, background questions, targeted stop, project form")


if __name__ == "__main__":
    launcher = Path(__file__).with_name("main.py")
    result = subprocess.run(
        [sys.executable, str(launcher), "--help"],
        capture_output=True, text=True, check=True, timeout=10,
    )
    assert "Codex Light TUI" in result.stdout and "--handoff-ready" in result.stdout
    asyncio.run(run())
