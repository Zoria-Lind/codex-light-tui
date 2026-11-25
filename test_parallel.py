"""Regression coverage for independently running chats in one TUI."""

import asyncio
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
from unittest.mock import patch

from textual.events import MouseScrollUp
from textual.widgets import Input, OptionList, TextArea

from backend import CodexError
from dialogs import Approval, Picker, ProjectDialog, Questions, StatusDialog
from main import CodexLight
from transcript import ChatMessage


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
        self.reject_compaction = False
        self.quota = None
        self.history = {}
        self.reject_resume = set()

    async def request(self, method, params=None):
        self.requests.append((method, params))
        if method == "model/list":
            return {"data": [
                {"model": "main-model", "displayName": "Main Model", "isDefault": True,
                 "defaultReasoningEffort": "low", "supportedReasoningEfforts": [{"reasoningEffort": "low"}]},
                {"model": "side-model", "displayName": "Side Model", "defaultReasoningEffort": "medium",
                 "supportedReasoningEfforts": [{"reasoningEffort": level} for level in ("medium", "high")]},
            ]}
        if method == "account/rateLimits/read":
            if isinstance(self.quota, Exception):
                raise self.quota
            return {"rateLimits": self.quota}
        if method == "thread/list":
            return {"data": list(self.rows.values())}
        if method == "thread/turns/list":
            return {"data": self.history.get(params["threadId"], [])}
        if method == "project/list":
            return {"data": []}
        if method == "project/create":
            return {"project": {"id": "new-project-id", **params}}
        if method == "thread/start":
            thread = {"id": "project-thread", "cwd": params["cwd"], "projectId": params.get("projectId"), "updatedAt": 1}
            self.rows[thread["id"]] = thread
            return {"thread": thread}
        tid = params["threadId"]
        if method == "thread/fork":
            branch = {**self.rows[tid], "id": f"side-{tid}", "ephemeral": params["ephemeral"]}
            self.rows[branch["id"]] = branch
            return {"thread": branch}
        if method == "thread/compact/start":
            if self.reject_compaction:
                raise CodexError("mock compaction refusal")
            await self.app.backend_event({"method": "turn/started", "params": {
                "threadId": tid, "turn": {"id": f"compact-{tid}"},
            }})
            return {}
        if method == "thread/read" and tid == "a" and self.wait_for_history is not None:
            self.history_started.set()
            await self.wait_for_history.wait()
        if method in {"thread/read", "thread/resume"}:
            if method == "thread/resume" and tid in self.reject_resume:
                raise CodexError("saved side thread unavailable")
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


async def run_commands():
    with patch("main.read_state", return_value={}):
        app = TestApp()
    rows = {tid: {"id": tid, "name": tid, "cwd": str(Path.cwd()), "updatedAt": 1} for tid in ("a", "b")}
    backend = app.backend = TestBackend(app, rows)
    with patch("main.desktop_preferences", return_value=({}, {}, {}, [], {})):
        async with app.run_test(size=(144, 44)) as pilot:
            app.rows = list(rows.values())
            app.connected = True
            await app.open_thread("a").wait()
            main = app.chat
            prompt = app.query_one("#prompt", TextArea)
            menu = app.query_one("#slash-commands", OptionList)
            main.attachments.append("keep.png")
            prompt.focus()
            await pilot.press("slash")
            assert menu.display and menu.option_count == 3
            await pilot.press("escape")
            assert not menu.display and prompt.text == "/"
            prompt.load_text("/c")
            await pilot.pause()
            await pilot.press("enter")
            await app.workers.wait_for_complete()
            assert main.compacting and main.active_turn == "compact-a"
            assert main.attachments == ["keep.png"] and not prompt.text
            await app.backend_event({"method": "turn/completed", "params": {
                "threadId": "a", "turn": {"id": "compact-a", "status": "completed"},
            }})
            assert not main.busy and main.compact_status == "上下文压缩完成"
            backend.reject_compaction = True
            await app.compact_chat(main).wait()
            assert not main.busy and "mock compaction refusal" in main.compact_status

            prompt.focus()
            await pilot.press("slash", "down", "enter")
            assert app.btw.display and app.btw.prompt.has_focus
            app.btw.prompt.load_text("retained side draft")
            app.btw.prompt.move_cursor((0, len("retained side draft")))
            await pilot.press("shift+enter")
            assert app.btw.prompt.text == "retained side draft\n"
            await pilot.click("#btw-toggle")
            await pilot.pause(.25)
            await pilot.click("#btw-toggle")
            assert app.btw.display
            assert app.btw.prompt.text == "retained side draft\n"
            app.btw.prompt.clear()
            prompt.load_text("/btw side question")
            await app.action_send_prompt().wait()
            await app.btw.workers.wait_for_complete()
            side = app.btw.current
            assert side.busy and side.thread_id == "side-a"
            assert ("thread/fork", {"threadId": "a", "ephemeral": False, "excludeTurns": True}) in backend.requests
            prompt.load_text("Look at /notes/file.txt")
            await app.action_send_prompt().wait()
            assert main.busy and backend.active == {"a", "side-a"}
            await app.backend_event({"method": "item/completed", "params": {
                "threadId": "side-a", "item": {"id": "side-answer", "type": "agentMessage", "text": "only in the side panel"},
            }})
            assert side.messages == [("Codex", "only in the side panel")] and not main.messages
            sample = "**这是一段用于验证窄栏自动换行的中文。**" * 6 + " END-OF-LINE"
            await app.backend_event({"method": "item/completed", "params": {
                "threadId": "side-a", "item": {"id": "wrap-check", "type": "agentMessage", "text": sample},
            }})
            await pilot.pause()
            row = app.btw.query_one("#btw-history").children[-1]
            assert isinstance(row, ChatMessage) and row.has_class("from-codex")
            wide = row.render().plain
            await pilot.resize_terminal(95, 44)
            await pilot.pause()
            narrow = row.render().plain
            assert narrow.count("\n") > wide.count("\n") and "LINE" in narrow, (wide, narrow)
            assert not app.btw.query_one("#btw-history").show_horizontal_scrollbar
            assert any(p["input"][0]["text"].startswith("Look at /notes/file.txt")
                       for method, p in backend.requests if method == "turn/start" and p["threadId"] == "a")
            await app.open_thread("b").wait()
            assert app.btw.current is not side
            await app.open_thread("a").wait()
            assert app.btw.current is side and side.messages
    print("PASS: slash menu, compaction, BTW draft and isolation, literal slash")


async def run_model_and_status():
    with patch("main.read_state", return_value={}), patch("main.desktop_preferences", return_value=({}, {}, {}, [], {})):
        app = TestApp()
        rows = {tid: {"id": tid, "name": tid, "cwd": str(Path.cwd()), "updatedAt": 1} for tid in ("a", "b")}
        backend = app.backend = TestBackend(app, rows)
        async with app.run_test(size=(144, 44)) as pilot:
            app.rows = list(rows.values())
            app.connected = True
            await app.open_thread("a").wait()
            main = app.chat
            prompt = app.query_one("#prompt", TextArea)

            async def pick(button, index, caption):
                if button.startswith("/"):
                    app.btw.prompt.load_text(button)
                    app.btw.prompt.focus()
                    await pilot.press("enter")
                else:
                    await pilot.click(button)
                async with asyncio.timeout(3):
                    while True:
                        options = next(iter(app.screen.query("#picker-options")), None)
                        if isinstance(app.screen, Picker) and options and options.has_focus:
                            break
                        await asyncio.sleep(.01)
                assert app.screen.caption == caption
                await pilot.hover("#cancel-dialog")
                await pilot.press("home", *(["down"] * index), "enter")
                await app.workers.wait_for_complete()

            async def status():
                prompt.load_text("/status")
                await app.action_send_prompt().wait()
                await app.workers.wait_for_complete()
                assert isinstance(app.screen, StatusDialog)
                detail = app.screen.query_one("#status-details").render()
                await pilot.press("escape")
                return str(detail)

            detail = await status()
            assert "共享后端" in detail and "缓存命中率 · 暂无数据" in detail and "账号额度 · 暂无数据" in detail
            await pick("#model-picker", 1, "选择模型")
            assert main.model == "main-model", main.model
            await pick("#effort-picker", 1, "选择思考强度")
            assert main.effort == "low", main.effort
            await pilot.click("#btw-toggle")
            await pick("/model", 2, "BTW · 选择模型")
            await pick("/effort", 2, "BTW · 选择思考强度")
            side = app.btw.current
            assert (main.model, main.effort) == ("main-model", "low")
            assert (side.model, side.effort) == ("side-model", "high")
            await pilot.click("#btw-toggle")
            assert not app.btw.display
            await pilot.pause(.25)
            await pilot.click("#btw-toggle")
            assert app.btw.display
            await app.open_thread("b").wait()
            assert app.btw.current is not side and not app.btw.current.model
            await app.open_thread("a").wait()
            assert (app.btw.current.model, app.btw.current.effort) == ("side-model", "high")

            prompt.load_text("/btw side settings test")
            await app.action_send_prompt().wait()
            await app.btw.workers.wait_for_complete()
            prompt.load_text("main settings test")
            await app.action_send_prompt().wait()
            calls = [(p["threadId"], p["model"], p["effort"]) for method, p in backend.requests if method == "turn/start"]
            assert calls == [("side-a", "side-model", "high"), ("a", "main-model", "low")]
            assert backend.active == {"a", "side-a"}
            main.usage = {"last": {"inputTokens": 20, "cachedInputTokens": 5},
                          "total": {"inputTokens": 100, "cachedInputTokens": 80}}
            backend.quota = {"primary": {"windowDurationMins": 300, "usedPercent": 12}}
            detail = await status()
            assert all(x in detail for x in ("Main Model", "low", "25.0%", "80.0%", "5h额度已用 12%")), detail
            backend.quota = CodexError("quota refusal")
            detail = await status()
            assert "上次读取值" in detail and "额度刷新失败：quota refusal" in detail
            app.connected = False
            reads = sum(method == "account/rateLimits/read" for method, _ in backend.requests)
            detail = await status()
            assert "未连接" in detail and "上次读取值" in detail
            assert reads == sum(method == "account/rateLimits/read" for method, _ in backend.requests)
    print("PASS: independent BTW model and effort, settings retained, /status empty, populated, failed refresh, offline")


async def run_btw_resume():
    rows = {"a": {"id": "a", "name": "a", "cwd": str(Path.cwd()), "updatedAt": 1}}
    with patch("main.read_state", return_value={}), patch("main.desktop_preferences", return_value=({}, {}, {}, [], {})):
        app = TestApp()
        backend = app.backend = TestBackend(app, rows)
        async with app.run_test(size=(95, 44)) as pilot:
            app.connected = True
            await app.open_thread("a").wait()
            app.chat.model, app.chat.effort = "side-model", "high"
            await pilot.click("#btw-toggle")
            await pilot.press("slash")
            menu = app.btw.query_one("#btw-commands", OptionList)
            assert menu.display and menu.option_count == 3
            app.btw.prompt.load_text("initial side question")
            await pilot.press("enter")
            await app.btw.workers.wait_for_complete()
            assert app.btw.current.thread_id == "side-a"
            sent = next(params for method, params in backend.requests if method == "turn/start")
            assert (sent["model"], sent["effort"]) == ("side-model", "high")
            assert app.btw.settings("main-model", "", "side-model", "high")["effort"] == "low"
            app.btw.current.effort = "medium"
            app.btw.save_settings(app.btw.current)
        app = TestApp()
        backend = app.backend = TestBackend(app, rows)
        formula = r"Inline \(i^2=-1\)" + "\n\n" + r"\[\begin{aligned}x&=2\\&=1+1\end{aligned}\]"
        backend.history["side-a"] = [
            {"id": "turn-side-a", "items": [{"type": "agentMessage", "text": formula}]},
            {"id": "original-main-turn", "items": [{"type": "agentMessage", "text": "not from side"}]},
        ]
        async with app.run_test(size=(95, 44)) as pilot:
            app.connected = True
            await app.open_thread("a").wait()
            await pilot.click("#btw-toggle")
            assert app.btw.current.effort == "medium"
            app.btw.prompt.load_text("/resume")
            await pilot.press("enter")
            await app.btw.workers.wait_for_complete()
            await pilot.pause()
            assert app.btw.current.attached and app.btw.current.thread_id == "side-a"
            assert app.btw.current.messages == [("Codex", formula)]
            row = app.btw.query_one("#btw-history").children[-1]
            text = row.render().plain
            assert "i²=-1" in text and "=2" in text and "=1+1" in text and "aligned" not in text, text
            await app.load_threads().wait()
            assert all(row["id"] != "side-a" for row in app.rows)
            app.btw.disconnected()
            backend.reject_resume.add("side-a")
            app.btw.prompt.load_text("unsent side draft")
            await app.btw.resume_current(app.btw.current).wait()
            assert "saved side thread unavailable" in app.btw.current.status
            assert app.btw.prompt.text == "unsent side draft" and app.btw.current.thread_id == "side-a"
            assert not any(method == "thread/fork" for method, _ in backend.requests)
    print("PASS: inherited settings, one saved BTW branch, restored scope and displayed math")


async def run_jump_latest():
    with patch("main.read_state", return_value={}):
        app = TestApp()
    rows = {"a": {"id": "a", "name": "short title", "cwd": str(Path.cwd()), "updatedAt": 1}}
    app.backend = TestBackend(app, rows)
    async with app.run_test(size=(95, 40)) as pilot:
        app.connected = True
        await app.open_thread("a").wait()
        app.set_btw_visible(True)
        messages = [("Codex", f"History line {index}") for index in range(24)]
        app.chat.messages = list(messages)
        await app.paint_history(app.chat)
        side = app.btw.current
        side.messages = list(messages)
        app.btw.threads["scroll-test-side"] = side
        app.btw.paint()
        main_log, side_log = (app.query_one(selector) for selector in ("#history", "#btw-history"))
        main_button, side_button = (app.query_one(selector) for selector in ("#history-latest", "#btw-history-latest"))
        async with asyncio.timeout(3):
            while any(not log.max_scroll_y or not log.is_vertical_scroll_end for log in (main_log, side_log)):
                await pilot.pause(.025)
        # Let the initial history paints finish their deferred scroll-to-end callbacks.
        await pilot.pause()
        assert not main_button.display and not side_button.display

        main_size, side_size = main_log.size, side_log.size
        for log in (main_log, side_log):
            log.post_message(MouseScrollUp(log, 2, 2, 0, 0, 0, False, False, False))
        async with asyncio.timeout(3):
            while any(not button.display or button.region.height != 3 for button in (main_button, side_button)):
                await pilot.pause(.025)
        assert (main_log.size, side_log.size) == (main_size, side_size)
        for button in (main_button, side_button):
            outline = [line.text for line in button.render_lines(button.region.size.region)]
            assert outline == ["╭───╮", "│ ↓ │", "╰───╯"], outline

        # Being a single line above the bottom should also preserve the reading position.
        main_log.scroll_to(y=main_log.max_scroll_y - 1, animate=False, immediate=True)
        positions = main_log.scroll_y, side_log.scroll_y
        await app.record_message("Codex", "New main reply while scrolling")
        await app.backend_event({"method": "item/completed", "params": {
            "threadId": "scroll-test-side",
            "item": {"id": "new-side-reply", "type": "agentMessage", "text": "New side reply while scrolling"},
        }})
        await pilot.pause()
        assert (main_log.scroll_y, side_log.scroll_y) == positions
        assert main_button.display and side_button.display

        assert await pilot.click(main_button)
        assert main_log.is_vertical_scroll_end and not main_button.display
        assert side_log.scroll_y == positions[1] and side_button.display
        assert await pilot.click(side_button)
        assert side_log.is_vertical_scroll_end and not side_button.display
        previous_sizes = main_log.size.height, side_log.size.height
        app.chat.live["main-preview"] = "Live main reply\n\nMore preview"
        app.live_dirty = True
        side.live["side-preview"] = "Live side reply\n\nMore preview"
        app.btw.live_dirty = True
        app.render_live()
        async with asyncio.timeout(3):
            while (main_log.size.height, side_log.size.height) == previous_sizes:
                await pilot.pause(.025)
        assert main_log.is_vertical_scroll_end and side_log.is_vertical_scroll_end
        await app.record_message("Codex", "Follow the next live reply")
        async with asyncio.timeout(3):
            while not main_log.is_vertical_scroll_end or main_button.display:
                await pilot.pause(.025)
    print("PASS: both latest buttons, wheel events, narrow layout and scroll position during replies")


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
        async with asyncio.timeout(3):
            while True:
                name_input = next(iter(app.screen.query("#project-name").results(Input)), None)
                if isinstance(app.screen, ProjectDialog) and name_input and name_input.region:
                    break
                await pilot.pause(.01)
        name_input.value = "Example project"
        name_input.focus()
        await pilot.press("enter")
        await asyncio.wait_for(creation.wait(), 3)
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
    for case in (run, run_commands, run_model_and_status, run_btw_resume, run_jump_latest):
        with TemporaryDirectory(prefix="tui-test-") as isolated, patch("btw_state.INDEX_PATH", Path(isolated) / "btw.json"):
            asyncio.run(asyncio.wait_for(case(), 20))
