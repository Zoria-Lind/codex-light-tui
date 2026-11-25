"""One resumable conversation alongside each main thread."""

from dataclasses import dataclass, field

from rich.text import Text
from textual import on, work
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Button, Label, OptionList, Static, TextArea
from textual.widgets.option_list import Option

from backend import CodexError, ConnectionInterrupted
from btw_state import edit_index, read_index
from composer import Composer
from transcript import TranscriptPane, markdown, message_block

COMMANDS = {"/model": "支线模型", "/effort": "支线思考强度", "/resume": "恢复当前支线"}

@dataclass
class SideChat:
    parent_id: str
    draft: str = ""
    thread_id: str | None = None
    turn_id: str | None = None
    busy: bool = False
    restoring: bool = False
    attached: bool = False
    status: str = ""
    model: str = ""
    effort: str = ""
    first_turn: str | None = None
    message_revision: int = 0
    live: dict = field(default_factory=dict)
    messages: list = field(default_factory=list)


class BtwPanel(Vertical):
    def __init__(self):
        super().__init__(id="btw-panel")
        self.parent_id = None
        self.chats = {}
        self.threads = {}
        self.live_dirty = False
        self.index = {}
        self.index_error = ""
        self.reload_index()

    def compose(self):
        yield Label("BTW · 支线", id="btw-title")
        yield TranscriptPane("btw-history")
        yield Static("", markup=False, id="btw-live")
        yield Label("", markup=False, id="btw-status")
        yield OptionList(id="btw-commands")
        yield Composer(submit=self.submit, menu_key=self.menu_key, placeholder="问一个问题，或输入 / 查看命令", id="btw-input")
        with Horizontal(id="btw-actions"):
            yield Button("停止", id="btw-stop", disabled=True)
            yield Button("发送", id="btw-send", variant="primary")

    @property
    def current(self):
        return self.chats.get(self.parent_id)

    @property
    def prompt(self):
        return self.query_one("#btw-input", Composer)

    def reload_index(self):
        try:
            self.index = read_index()
            self.index_error = ""
        except (ValueError, OSError) as error:
            self.index_error = f"无法读取 BTW 本地索引：{error}"

    def get_chat(self):
        if self.parent_id not in self.chats:
            self.reload_index()
            saved = self.index.get(self.parent_id, {})
            self.chats[self.parent_id] = SideChat(
                self.parent_id, thread_id=saved.get("thread_id"), first_turn=saved.get("first_turn"),
                model=saved.get("model") or "", effort=saved.get("effort") or "",
            )
        return self.chats[self.parent_id]

    def save(self, parent_id, **fields):
        with edit_index() as index:
            index.setdefault(parent_id, {}).update(fields)
        self.index = index

    def save_settings(self, state):
        try:
            self.save(state.parent_id, model=state.model, effort=state.effort)
        except (OSError, ValueError) as error:
            state.status = f"设置已用于当前窗口，保存失败：{error}"
        self.update_controls()

    def saved_threads(self):
        return {row.get("thread_id") for row in self.index.values()}

    def set_parent(self, parent_id):
        if self.parent_id == parent_id:
            return
        if self.current:
            self.current.draft = self.prompt.text
        self.parent_id = parent_id
        if self.display and parent_id:
            self.get_chat()
        self.paint()

    def paint(self):
        log = self.query_one("#btw-history", VerticalScroll)
        log.remove_children()
        state = self.current
        if state:
            log.mount_all(message_block(*message) for message in state.messages)
            self.call_after_refresh(log.scroll_end, animate=False)
        self.prompt.load_text(state.draft if state else "")
        self.live_dirty = True
        self.update_controls()

    def open(self, question=""):
        self.display = True
        if self.parent_id and not self.current:
            self.get_chat()
            self.paint()
        self.update_controls()
        if not question:
            if self.parent_id:
                self.prompt.focus()
            return True
        if not self.current or self.prompt.text or not self.app.connected or self.current.busy or self.current.restoring:
            return False
        self.prompt.load_text(question)
        self.prompt.focus()
        self.submit()
        return True

    def update_controls(self):
        state = self.current
        self.prompt.disabled = self.parent_id is None
        self.query_one("#btw-send", Button).disabled = not (state and self.app.connected and not state.busy and not state.restoring)
        self.query_one("#btw-stop", Button).disabled = not (state and self.app.connected and state.turn_id)
        hint = "输入 / 查看命令"
        if state and state.thread_id and not state.attached:
            hint = "输入 /resume 恢复已保存的支线"
        text = "先打开一条已保存的主聊天" if not self.parent_id else (
            (state.status if state else "") or self.index_error or hint
        )
        self.query_one("#btw-status", Label).update(Text(text))

    def render_live(self):
        if not self.display or not self.live_dirty:
            return
        self.live_dirty = False
        state = self.current
        preview = "\n".join(state.live.values())[-1500:] if state else ""
        widget = next(self.query("#btw-live").results(Static), None)
        if widget is None:
            return
        widget.display = bool(preview)
        if preview:
            widget.update(markdown(preview))

    @on(Button.Pressed, "#btw-send")
    def input_submitted(self, event):
        event.stop()
        self.submit()

    @on(TextArea.Changed, "#btw-input")
    def update_menu(self, event):
        menu = self.query_one("#btw-commands", OptionList)
        text = event.text_area.text
        options = [Option(f"{command}   {label}", id=command) for command, label in COMMANDS.items()
                   if text.startswith("/") and command.startswith(text)]
        menu.clear_options().add_options(options)
        menu.display = bool(options)
        menu.highlighted = 0 if options else None

    def menu_key(self, key):
        menu = self.query_one("#btw-commands", OptionList)
        if not menu.display:
            return False
        if key == "escape":
            menu.display = False
        elif key == "up":
            menu.action_cursor_up()
        else:
            menu.action_cursor_down()
        return True

    @on(OptionList.OptionSelected, "#btw-commands")
    def pick_command(self, event):
        self.run_command(event.option.id)

    def run_command(self, command):
        state = self.current
        if not state:
            return
        if not self.app.connected:
            state.status = "后端未连接，按 F5 重连后重试。"
            self.update_controls()
            return
        self.prompt.clear()
        self.query_one("#btw-commands").display = False
        self.prompt.focus()
        if command == "/resume":
            self.resume_current(state)
        else:
            self.app.choose_model_setting(command[1:], side=state)

    def settings(self, model, effort, main_model, main_effort):
        chosen = model or main_model
        info = self.app.model_info(chosen)
        level = effort or main_effort or info.get("defaultReasoningEffort")
        supported = {entry["reasoningEffort"] for entry in info.get("supportedReasoningEfforts", [])}
        if (supported and level not in supported) or (not supported and chosen != main_model and not effort):
            level = info.get("defaultReasoningEffort")
        return {key: value for key, value in (("model", chosen), ("effort", level)) if value}

    def submit(self):
        state = self.current
        original = self.prompt.text
        command = original.rstrip()
        if command in COMMANDS:
            self.run_command(command)
            return
        menu = self.query_one("#btw-commands", OptionList)
        if menu.display and menu.highlighted_option and menu.highlighted_option.id.startswith(original):
            self.run_command(menu.highlighted_option.id)
            return
        if not state or not original.strip() or state.busy or state.restoring or not self.app.connected:
            return
        state.busy = True
        state.status = "正在发送…"
        self.update_controls()
        main = self.app.chat
        main_model = main.model or main.server_model or (main.current or {}).get("model")
        main_effort = self.app.effective_effort(main, self.app.model_info(main_model) if main_model else {})
        self.send_question(state, original, state.model, state.effort, main_model, main_effort)

    async def attach(self, state):
        thread_id = state.thread_id
        self.threads[thread_id] = state
        try:
            result = await self.app.backend.request("thread/resume", {"threadId": thread_id, "excludeTurns": True})
            if (result["thread"].get("status") or {}).get("type") == "active" and not state.turn_id:
                latest = await self.app.backend.request("thread/turns/list", {
                    "threadId": thread_id, "limit": 1, "itemsView": "summary", "sortDirection": "desc",
                })
                turn = next(iter(latest["data"]), None)
                if turn and turn.get("status") == "inProgress":
                    state.turn_id = turn["id"]
            await self.load_history(state)
        except (OSError, CodexError):
            self.threads.pop(thread_id, None)
            raise
        state.attached = True

    async def load_history(self, state):
        if not state.first_turn:
            return
        revision = state.message_revision
        entries = []
        cursor = None
        seen = set()
        while True:
            page = await self.app.backend.request("thread/turns/list", {
                "threadId": state.thread_id, "limit": 30, "itemsView": "full", "sortDirection": "desc", "cursor": cursor,
            })
            found = False
            for turn in page["data"]:
                if len(entries) < 60:
                    entries.extend(self.app.item_text(item) for item in reversed(turn.get("items", []))
                                   if item.get("type") in {"agentMessage", "userMessage"})
                if turn["id"] == state.first_turn:
                    found = True
                    break
            if found:
                if revision != state.message_revision:
                    return
                state.messages = list(reversed(entries[:60]))
                if self.current is state:
                    state.draft = self.prompt.text
                    self.paint()
                return
            cursor = page.get("nextCursor")
            if not cursor or cursor in seen:
                raise CodexError("找不到支线历史起点，已保留当前草稿。")
            seen.add(cursor)

    @work
    async def resume_current(self, state):
        if state.restoring or state.busy:
            return
        if not state.thread_id:
            state.status = self.index_error or "当前主聊天还没有已保存的支线。"
            self.update_controls()
            return
        state.restoring = True
        try:
            if not state.attached:
                await self.attach(state)
            state.busy = bool(state.turn_id)
            state.status = "已接回正在运行的支线" if state.busy else "已恢复原支线"
        except (CodexError, OSError) as error:
            state.status = f"恢复失败：{error}"
        finally:
            state.restoring = False
            if self.current is state:
                self.update_controls()

    def mark_first_turn(self, state, turn_id):
        if state.first_turn is None:
            state.first_turn = turn_id
        if self.index.get(state.parent_id, {}).get("first_turn") != state.first_turn:
            try:
                self.save(state.parent_id, thread_id=state.thread_id, first_turn=state.first_turn)
            except (OSError, ValueError) as error:
                state.status = f"无法保存支线历史起点：{error}"

    @work
    async def send_question(self, state, original, model, effort, main_model, main_effort):
        try:
            if not self.app.models:
                await self.app.load_model_catalog()
            parent = self.app.chats.get(state.parent_id)
            if parent and not main_model:
                await self.app.subscribe_chat(parent)
                main_model = parent.server_model or (parent.current or {}).get("model")
                main_effort = main_effort or parent.server_effort
            if not state.thread_id:
                with edit_index() as index:
                    record = index.setdefault(state.parent_id, {})
                    if record.get("thread_id"):
                        state.thread_id = record["thread_id"]
                        state.first_turn = record.get("first_turn")
                    else:
                        result = await self.app.backend.request("thread/fork", {
                            "threadId": state.parent_id, "ephemeral": False, "excludeTurns": True,
                        })
                        state.thread_id = record["thread_id"] = result["thread"]["id"]
                        main_model = main_model or result.get("model") or result["thread"].get("model")
                        main_effort = main_effort or result.get("reasoningEffort") or result["thread"].get("reasoningEffort")
                        state.attached = True
                        self.threads[state.thread_id] = state
                self.index = index
            if not state.attached:
                await self.attach(state)
            if state.turn_id:
                state.status = "支线仍在运行，草稿已保留。"
                return
            main_model = main_model or self.app.model_info().get("model")
            settings = self.settings(model, effort, main_model, main_effort)
            result = await self.app.backend.request("turn/start", {
                "threadId": state.thread_id, "input": [{"type": "text", "text": original.strip()}],
                "sandboxPolicy": {"type": "readOnly", "networkAccess": False}, "approvalPolicy": "never",
                **settings,
            })
            self.mark_first_turn(state, result["turn"]["id"])
            if state.busy:
                state.turn_id = result["turn"]["id"]
            if self.current is state and self.prompt.text == original:
                self.prompt.clear()
            elif state.draft == original:
                state.draft = ""
        except (CodexError, OSError, ValueError) as error:
            state.busy = bool(state.turn_id)
            state.status = ("发送结果未确认，草稿已保留" if isinstance(error, (ConnectionInterrupted, OSError))
                            else f"BTW 发送失败：{error}")
        finally:
            if self.current is state:
                self.update_controls()

    @on(Button.Pressed, "#btw-stop")
    def stop(self, event):
        event.stop()
        state = self.current
        if state and state.turn_id:
            self.stop_turn(state, state.thread_id, state.turn_id)

    @work
    async def stop_turn(self, state, thread_id, turn_id):
        try:
            await self.app.backend.request("turn/interrupt", {"threadId": thread_id, "turnId": turn_id})
        except (CodexError, OSError) as error:
            state.status = str(error)
            if self.current is state:
                self.update_controls()

    async def backend_event(self, thread_id, event):
        state = self.threads.get(thread_id)
        if not state:
            return False
        method, params = event["method"], event.get("params") or {}
        if "id" in event:
            try:
                await self.app.backend.send({"id": event["id"], "error": {
                    "code": -32601, "message": "Interactive tool requests are unavailable in BTW",
                }})
            except (CodexError, OSError):
                pass
        elif method == "turn/started":
            state.turn_id = params["turn"]["id"]
            state.busy = True
            state.status = "Codex 正在回复…"
            self.mark_first_turn(state, state.turn_id)
        elif method == "item/agentMessage/delta":
            item = params["itemId"]
            state.live[item] = (state.live.get(item, "") + params["delta"])[-1500:]
            if self.current is state:
                self.live_dirty = True
            return True
        elif method == "item/completed":
            item = params["item"]
            state.live.pop(item["id"], None)
            if item.get("type") in {"agentMessage", "userMessage"}:
                message = self.app.item_text(item)
                state.message_revision += 1
                state.messages.append(message)
                overflow = len(state.messages) > 60
                if overflow:
                    state.messages.pop(0)
                if self.current is state:
                    log = self.query_one("#btw-history", VerticalScroll)
                    if overflow and log.children:
                        await log.children[0].remove()
                    if self.current is state:
                        await log.mount(message_block(*message))
        elif method == "turn/completed":
            turn = params["turn"]
            if state.turn_id and state.turn_id != turn["id"]:
                return True
            state.turn_id = None
            state.busy = False
            state.live.clear()
            state.status = (turn.get("error") or {}).get("message") or {
                "completed": "回复完成", "interrupted": "已停止", "failed": "回复失败",
            }.get(turn["status"], turn["status"])
        if self.current is state:
            self.live_dirty = True
            self.update_controls()
        return True

    def disconnected(self):
        for state in self.threads.values():
            state.turn_id = None
            state.attached = False
            state.busy = False
            state.live.clear()
            state.status = "连接已断开 · 重连后可用 /resume 恢复"
        self.threads.clear()
        self.live_dirty = True
        self.update_controls()
