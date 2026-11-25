import argparse
import asyncio
from dataclasses import dataclass, field
from datetime import datetime
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import webbrowser
from uuid import UUID, uuid4

from rich.text import Text
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.theme import Theme
from textual.widgets import Button, Input, Label, OptionList, Static, TextArea, Tree
from textual.widgets.option_list import Option
from textual.worker import WorkerCancelled

from backend import Backend, CodexError, ConnectionInterrupted, codex_environment
from btw import BtwPanel
from composer import Composer, copy_clipboard, read_clipboard
from dialogs import Approval, PathDialog, Picker, ProjectDialog, Questions, Settings, StatusDialog
from shared_state import RESIDENT_TASK, STATUS, read_endpoint
from sidebar import catalog, desktop_preferences
from transcript import ToolBlock, TranscriptPane, markdown, message_block, submitted_text, tool_detail


STATE_PATH = Path(__file__).with_name(".ui-state.json")
PAGE_SIZE = 24
IMAGE_TYPES = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}
IMAGE_LINK = re.compile(r'\]\(<?([^\r\n<>]+?\.(?:png|jpe?g|gif|webp|bmp))>?\)', re.I)
THREAD_LIST_OPTIONS = {
    "limit": 75, "sortKey": "recency_at", "useStateDbOnly": True,
    "modelProviders": [], "sourceKinds": ["cli", "vscode", "appServer", "exec"],
}
QUESTION_CONFIG = {"features.default_mode_request_user_input": True}
SLASH_COMMANDS = {"/compact": "压缩当前上下文", "/btw": "打开右侧支线", "/status": "查看状态与用量"}
FOREST = Theme(
    name="codex-forest", primary="#24543d", secondary="#375747", accent="#87ba9c",
    foreground="#efefed", background="#121412", surface="#1a1e1a", panel="#202620",
    success="#8bcf98", error="#ef9a9a", warning="#d5b77d", dark=True,
    variables={
        "block-cursor-background": "#254333",
        "block-cursor-foreground": "#f0f5f0",
        "block-hover-background": "#25352a",
    },
)


def file_stamp(path):
    if not path:
        return None
    try:
        info = Path(path).stat()
        return info.st_mtime_ns, info.st_size
    except OSError:
        return None


def read_state():
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def clean_label(value):
    return " ".join(value.split())


@dataclass(eq=False)
class ChatState:
    current: dict | None = None
    draft: str = ""
    project_id: str | None = None
    model: str = ""
    effort: str = ""
    server_model: str = ""
    server_effort: str = ""
    permission: str = ""
    usage: dict = field(default_factory=dict)
    attachments: list = field(default_factory=list)
    images: list = field(default_factory=list)
    messages: list = field(default_factory=list)
    live: dict = field(default_factory=dict)
    file_changes: dict = field(default_factory=dict)
    requests: dict = field(default_factory=dict)
    active_turn: str | None = None
    busy: bool = False
    awaiting_turn: bool = False
    compacting: bool = False
    compact_status: str = ""
    asking_enabled: bool = False
    loaded: bool = False
    history_cursor: str | None = None
    legacy_offset: int = 0
    rollout_stamp: tuple | None = None
    turn_stamp: tuple | None = None
    tail_id: str | None = None
    tail_size: int = 0
    message_revision: int = 0
    scroll_y: float | None = None


class CodexLight(App):
    CSS_PATH = "app.tcss"
    ENABLE_COMMAND_PALETTE = False
    BINDINGS = [
        ("ctrl+b", "sidebar", "侧栏"), ("ctrl+f", "search", "搜索"),
        ("f5", "refresh", "同步"), ("ctrl+enter", "send_prompt", "发送"),
        ("f2", "settings", "设置"), ("ctrl+q", "close_client", "退出"),
        Binding("ctrl+c", "copy_selection", "复制选中文字", show=False, priority=True),
    ]

    def __init__(self, handoff_thread=None, handoff_ready=None):
        super().__init__()
        self.handoff_thread = handoff_thread
        self.handoff_ready = handoff_ready
        self.backend = Backend(self.backend_event)
        self.state = read_state()
        self.expanded = set(self.state.get("expanded", []))
        self.rows = []
        self.projects = []
        self.sidebar_preferences = ({}, {}, {}, [], {})
        self.search_timer = None
        self.chat = ChatState()
        self.chats = {}
        self.drafts = {}
        self.project_creation_keys = {}
        self.project = None
        self.models = []
        self.quota = {}
        self.live_dirty = False
        self.activity_step = 0
        self.connected = False
        self.subscriptions = set()
        self.reconnect_threads = set()
        self.subscription_lock = asyncio.Lock()
        self.history_lock = asyncio.Lock()
        self.request_dialog = None
        self.connecting = False
        self.reconnect_at = 0.0
        self.closing = False
        self.syncing = False
        self.loading_history = False
        self.sidebar_stamp = None
        self.list_stamp = None
        self.last_full_poll = 0.0
        self.pasting = False
        self.btw = BtwPanel()
        self.desktop_state_path = Path(codex_environment().get("CODEX_HOME") or Path.home() / ".codex") / ".codex-global-state.json"

    def compose(self) -> ComposeResult:
        with Horizontal(id="bar"):
            yield Button("☰ 项目", id="sidebar-toggle")
            yield Label("  Codex", id="title")
            yield Button("同步", id="refresh")
            yield Button("用量", id="quota")
            yield Button("桌面 ↗", id="desktop")
            yield Button("ChatGPT ↗", id="chatgpt")
            yield Button("BTW ▸", id="btw-toggle", tooltip="展开或收起右侧支线")
        with Horizontal(id="body"):
            with Vertical(id="sidebar"):
                yield Input(placeholder="搜索聊天标题", id="search")
                tree = Tree("项目", id="tree")
                tree.show_root = False
                tree.root.expand()
                yield tree
                yield Button("＋ 新聊天", id="new-chat")
                yield Button("＋ 新建项目", id="new-project")
            with Vertical(id="main"):
                with Horizontal(id="thread-bar"):
                    yield Label("选中一条聊天继续", id="thread-title")
                    yield Button("待回答", id="pending-request")
                    yield Button("更早消息", id="more-turns", disabled=True)
                yield TranscriptPane("history")
                yield Static("", id="live", markup=False)
                yield Label("", id="activity", markup=False)
                yield Label("", id="attachments", markup=False)
                yield OptionList(id="slash-commands")
                yield Composer(id="prompt", placeholder="输入消息")
                with Horizontal(id="compose-actions"):
                    yield Label("Enter 发送 · Shift+Enter 换行 · / 命令", id="compose-hint")
                    yield Button("附件", id="attach")
                    yield Button("粘贴图", id="paste-image", tooltip="粘贴 Windows 剪贴板中的图片")
                    yield Button("移除", id="remove-attachment", disabled=True, tooltip="选择一项待发送附件移除")
                    yield Button("清空", id="clear-attachments", disabled=True, tooltip="清空待发送附件")
                    yield Button("看图", id="image")
                    yield Button("停止", id="stop", disabled=True)
                    yield Button("发送", id="send", variant="primary", disabled=True)
            yield self.btw
        yield Label("正在连接本机 Codex…", id="status", markup=False)
        with Horizontal(id="bottom-bar"):
            yield Button("⚙ 设置", id="settings", tooltip="会话权限 · F2")
            yield Label("", id="usage-line", markup=False)
            yield Button("模型 ▾", id="model-picker")
            yield Button("思考强度 ▾", id="effort-picker")

    def on_mount(self):
        self.register_theme(FOREST)
        self.theme = FOREST.name
        if self.state.get("sidebar_hidden"):
            self.query_one("#sidebar").display = False
        self.query_one("#live").display = False
        self.query_one("#activity").display = False
        self.query_one("#attachments").display = False
        self.query_one("#pending-request").display = False
        self.query_one("#slash-commands").display = False
        self.btw.display = False
        self.btw.update_controls()
        self.set_interval(0.15, self.render_live)
        self.set_interval(0.35, self.render_activity)
        self.set_interval(1, self.sync_local)
        self.connect()

    def status(self, message):
        self.query_one("#status", Label).update(Text(str(message)))

    def resident_label(self):
        try:
            if not self.backend.shared_url or self.backend.shared_url != read_endpoint():
                return ""
            state = json.loads(STATUS.read_text(encoding="utf-8"))
            backend, bridge = state.get("backend_pid"), state.get("bridge_pid")
            if state.get("phase") == "登录共享后端已就绪" and isinstance(backend, int) and isinstance(bridge, int):
                return f"后端 PID {backend} · 桥接 PID {bridge}"
        except (OSError, ValueError, KeyError):
            pass
        return ""

    def check_action(self, action, parameters):
        if action == "copy_selection":
            return self.screen is self.screen_stack[0] and bool(self.screen.get_selected_text())
        return super().check_action(action, parameters)

    @work
    async def action_copy_selection(self):
        selected = self.screen.get_selected_text()
        if selected:
            try:
                await copy_clipboard(selected)
                self.status("已复制选中文字")
            except OSError as error:
                self.status(error)

    def render_activity(self):
        label = next(self.query("#activity").results(Label), None)
        if label is None:
            return
        label.display = self.connected and bool(self.chat.active_turn or self.chat.awaiting_turn)
        if label.display:
            dots = "..."[:self.activity_step % 3 + 1]
            activity = "正在压缩上下文" if self.chat.compacting else (
                "Codex 正在工作" if self.chat.active_turn else "正在等待 Codex 确认"
            )
            label.update(Text(f"{activity}{dots}"))
            self.activity_step += 1
        else:
            self.activity_step = 0

    @work
    async def connect(self):
        if self.connecting:
            return
        self.connecting = True
        self.update_send_controls()
        try:
            self.status("正在连接本机 Codex…")
            if self.backend.process is not None or self.backend.socket is not None:
                await self.backend.close()
            if self.handoff_ready and not self.backend.shared_url:
                raise CodexError("桌面接力需要共享后端。")
            await self.backend.start()
            self.connected = True
            self.btw.update_controls()
            self.update_send_controls()
            mode = "共享 Codex 后端" if self.backend.shared_url else "本机 Codex"
            resident = self.resident_label()
            self.query_one("#title", Label).tooltip = f"{RESIDENT_TASK}\n{resident}" if resident else None
            self.status(f"已连接{mode} · {resident or '请选择项目或聊天'}")
            self.load_threads(refresh_projects=True)
            if self.handoff_ready:
                await self.open_thread(self.handoff_thread).wait()
                if self.handoff_thread not in self.subscriptions or self.chat.busy:
                    raise CodexError("未能接好桌面的聊天。")
                self.status("已从共享后端打开桌面聊天。")
                self.handoff_ready.touch(exist_ok=False)
            elif self.backend.shared_url and self.chat.current and self.chat.current["id"]:
                self.open_thread(self.chat.current["id"])
            if self.backend.shared_url:
                self.restore_subscriptions()
        except (CodexError, OSError) as error:
            self.connected = False
            self.btw.update_controls()
            self.status(str(error))
            await self.backend.close()
            self.status(f"连接失败，草稿保留 · F5 重连 · {error}")
        finally:
            self.connecting = False
            self.reconnect_at = time.monotonic() + 8
            self.update_send_controls()

    @work(exclusive=True, group="list")
    async def load_threads(self, refresh_projects=False, automatic=False):
        if not self.connected:
            return
        layout_changed = False
        if refresh_projects:
            self.sidebar_stamp = await asyncio.to_thread(file_stamp, self.desktop_state_path)
            preferences = await asyncio.to_thread(desktop_preferences)
            layout_changed = preferences != self.sidebar_preferences
            self.sidebar_preferences = preferences
            try:
                projects = []
                cursor = None
                while True:
                    page = await self.backend.request("project/list", {"limit": 100, "cursor": cursor})
                    projects.extend(page["data"])
                    cursor = page.get("nextCursor")
                    if not cursor:
                        break
                fields = ("id", "name", "roots", "metadata")
                layout_changed |= (
                    [tuple(p.get(f) for f in fields) for p in self.projects]
                    != [tuple(p.get(f) for f in fields) for p in projects]
                )
                self.projects = projects
            except (CodexError, OSError) as error:
                self.status(f"项目列表暂不可用：{error}")
        try:
            rows = await self.list_thread_rows(THREAD_LIST_OPTIONS)
        except (CodexError, OSError) as error:
            self.status(error)
            return
        assignments = self.sidebar_preferences[2]
        if assignments:
            listed = {row["id"] for row in rows}
            missing = [tid for tid in assignments if tid not in listed]
            for start in range(0, len(missing), 75):
                requested = set(missing[start:start + 75])
                try:
                    # Desktop may include specific noninteractive sessions in a project.
                    extra = await self.list_thread_rows({
                        **THREAD_LIST_OPTIONS, "includedThreadIds": list(requested),
                        "sourceKinds": THREAD_LIST_OPTIONS["sourceKinds"] + ["unknown"],
                    })
                    rows.extend(row for row in extra if row["id"] in requested)
                except (CodexError, OSError):
                    break
            rows.sort(key=lambda row: row.get("recencyAt") or row["updatedAt"], reverse=True)
        self.btw.reload_index()
        side_ids = self.btw.saved_threads()
        rows = [row for row in rows if row["id"] not in side_ids]
        signature = [(r["id"], r.get("name"), r.get("preview"), r.get("cwd"), r.get("projectId")) for r in rows]
        previous = [(r["id"], r.get("name"), r.get("preview"), r.get("cwd"), r.get("projectId")) for r in self.rows]
        self.rows = rows
        if not automatic or signature != previous or layout_changed:
            self.draw_tree(preserve_scroll=automatic)

    async def list_thread_rows(self, params):
        rows = {}
        seen = set()
        cursor = None
        while True:
            page = await self.backend.request("thread/list", {**params, "cursor": cursor})
            rows.update((row["id"], row) for row in page["data"])
            cursor = page.get("nextCursor")
            if not cursor:
                return list(rows.values())
            if cursor in seen:
                raise CodexError("聊天列表分页游标重复，已保留上次索引。")
            seen.add(cursor)

    def draw_tree(self, preserve_scroll=False):
        if self.closing or not self.screen_stack:
            return
        tree = next(self.screen_stack[0].query("#tree").results(Tree), None)
        if tree is None:
            return
        keyword = self.query_one("#search", Input).value.strip().casefold()
        rows = self.rows if not keyword else [row for row in self.rows if any(
            keyword in (row.get(key) or "").casefold() for key in ("name", "preview")
        )]
        scroll_y = tree.scroll_y
        collapsed = {(node.data or {}).get("section") for node in tree.root.children if not node.is_expanded}
        tree.clear()
        def add_chats(node, chats):
            for row in chats:
                name = clean_label(row.get("name") or row.get("preview") or "未命名聊天")
                state = self.chats.get(row["id"])
                title = Text()
                if state and state.requests:
                    methods = {event["method"] for event in state.requests.values()}
                    title.append("待回答 · " if "item/tool/requestUserInput" in methods else "待审批 · ", style="yellow")
                elif state and state.busy:
                    title.append("运行中 · ", style="green")
                title.append(name[:105])
                leaf = node.add_leaf(title, row)
                if self.chat.current and row["id"] == self.chat.current["id"]:
                    tree.move_cursor(leaf)

        for key, name, projects in catalog(self.projects, rows, self.sidebar_preferences):
            projects = [project for project in projects if not keyword or project["rows"]]
            if not projects:
                continue
            section = tree.root.add(Text(name, style="bold"), {"section": key})
            if keyword or key not in collapsed:
                section.expand()
            for project in projects:
                roots = project["roots"]
                cwd = roots[0] if roots else ""
                node = section.add(Text(project["name"]), {"project": cwd, "key": project["id"]})
                add_chats(node, project["rows"])
                if keyword or project["id"] in self.expanded or any(root in self.expanded for root in roots):
                    node.expand()
        if self.rows and not keyword:
            recent = tree.root.add(Text("最近", style="bold"), {"section": "recent"})
            if "recent" not in collapsed:
                recent.expand()
            add_chats(recent, self.rows[:20])
        if not tree.root.children:
            tree.root.add_leaf("没有找到聊天" if keyword else "暂无聊天")
        if preserve_scroll:
            self.call_after_refresh(tree.scroll_to, y=scroll_y, animate=False)

    @on(Input.Changed, "#search")
    def search_changed(self):
        if self.search_timer:
            self.search_timer.stop()
        self.search_timer = self.set_timer(0.28, self.draw_tree)

    @on(Tree.NodeExpanded)
    def expanded_node(self, event):
        data = event.node.data or {}
        if "project" in data and not self.query_one("#search", Input).value:
            self.expanded.add(data["key"])

    @on(Tree.NodeCollapsed)
    def collapsed_node(self, event):
        data = event.node.data or {}
        if "key" in data and not self.query_one("#search", Input).value:
            self.expanded.discard(data["key"])
            self.expanded.discard(data["project"])

    @on(Tree.NodeSelected)
    def select_tree_node(self, event):
        data = event.node.data or {}
        if "project" in data:
            self.project = data["project"]
            self.status(self.project)
        elif "id" in data:
            self.open_thread(data["id"])

    async def activate_chat(self, chat):
        if self.chat is chat:
            return
        previous = self.chat
        log = self.query_one("#history", VerticalScroll)
        prompt = self.query_one("#prompt", TextArea)
        previous.draft = prompt.text
        previous.scroll_y = None if log.is_vertical_scroll_end else log.scroll_y
        self.chat = chat
        prompt.load_text(chat.draft)
        self.btw.set_parent(chat.current.get("id"))
        self.project = chat.current.get("cwd")
        title = chat.current.get("name") or chat.current.get("preview") or "新聊天"
        self.query_one("#thread-title", Label).update(Text(clean_label(title)))
        self.query_one("#more-turns", Button).disabled = not chat.history_cursor or chat.busy
        self.live_dirty = True
        self.render_live()
        self.render_activity()
        self.render_attachments()
        self.update_usage()
        self.update_send_controls()
        if chat.compact_status:
            self.status(chat.compact_status)
        old_id = (previous.current or {}).get("id")
        if old_id and old_id in self.subscriptions and not previous.busy and not previous.requests:
            self.detach(old_id)
        await self.paint_history(chat)
        if chat.requests:
            self.show_pending_request()

    async def paint_history(self, chat, *, older=False):
        async with self.history_lock:
            if self.chat is not chat:
                return
            log = self.query_one("#history", VerticalScroll)
            chat.images = []
            widgets = [self.transcript_widget(role, body) for role, body in chat.messages]
            await log.remove_children()
            await log.mount_all(widgets)
            if self.chat is chat:
                if older:
                    self.call_after_refresh(log.scroll_home, animate=False)
                elif chat.scroll_y is None:
                    self.call_after_refresh(log.scroll_end, animate=False)
                else:
                    self.call_after_refresh(log.scroll_to, y=chat.scroll_y, animate=False)

    @work(exclusive=True, group="history")
    async def open_thread(self, thread_id, older=False, automatic=False):
        if not thread_id or not self.connected:
            return
        if older or automatic:
            if not self.chat.current or self.chat.current["id"] != thread_id or self.chat.busy:
                return
        chat = self.chats.get(thread_id)
        if chat is None:
            row = next((row for row in self.rows if row["id"] == thread_id), {"id": thread_id})
            chat = ChatState(current=dict(row))
        else:
            self.chats.pop(thread_id)
        self.chats[thread_id] = chat
        cached = [old for old in self.chats.values() if old is not chat and old.loaded and not old.busy and not old.requests]
        for old in cached[:-7]:
            old.messages.clear()
            old.loaded = False
            old.tail_id = old.history_cursor = None
            old.tail_size = 0
        self.loading_history = True
        try:
            if not older and not automatic:
                await self.activate_chat(chat)
            self.update_send_controls()
            if chat.busy and chat.loaded:
                return
            await self.read_history(thread_id, older, automatic)
        finally:
            self.loading_history = False
            self.update_send_controls()

    async def read_history(self, thread_id, older, automatic):
        chat = self.chat
        original_revision = chat.message_revision
        incremental = False
        if not older and not automatic:
            self.status("正在读取会话历史…")
        try:
            thread = self.chat.current if older else (
                await self.backend.request("thread/read", {"threadId": thread_id})
            )["thread"]
            if thread.get("historyMode") == "legacy":
                response = await self.backend.request("thread/read", {"threadId": thread_id, "includeTurns": True})
                all_turns = response["thread"]["turns"]
                end = chat.legacy_offset if older else len(all_turns)
                offset = max(0, end - PAGE_SIZE)
                turns = all_turns[offset:end]
                chat.legacy_offset = offset
                cursor = str(offset) if offset else None
            else:
                params = {"threadId": thread_id, "itemsView": "full", "limit": PAGE_SIZE, "sortDirection": "desc"}
                if older:
                    params["cursor"] = self.chat.history_cursor
                if automatic and chat.tail_id and chat.current and thread_id == chat.current["id"]:
                    tail = await self.backend.request("thread/turns/list", {**params, "limit": 2})
                    turns = tail["data"][::-1]
                    for index, turn in enumerate(turns):
                        if turn["id"] == chat.tail_id:
                            turns = turns[index:]
                            incremental = True
                            cursor = chat.history_cursor
                            break
                if not incremental:
                    page = await self.backend.request("thread/turns/list", params)
                    turns = page["data"][::-1]
                    cursor = page.get("nextCursor")
        except (CodexError, OSError) as error:
            if not automatic:
                self.status(error)
            return
        if self.chat is not chat:
            return
        changed = not chat.loaded
        self.chat.current = thread
        chat.project_id = thread.get("projectId") or chat.project_id
        if changed or not automatic:
            chat.rollout_stamp = await asyncio.to_thread(file_stamp, thread.get("path"))
            if self.chat is not chat:
                return
        if changed:
            self.chat.turn_stamp = None
        self.project = thread.get("cwd")
        self.chat.history_cursor = cursor
        self.query_one("#more-turns", Button).disabled = not cursor
        self.query_one("#thread-title", Label).update(Text(
            clean_label(thread.get("name") or thread.get("preview") or "未命名聊天")
        ))
        entries = [[entry for item in turn.get("items", []) if (entry := self.item_text(item))] for turn in turns]
        pairs = [entry for turn in entries for entry in turn]
        cap = 200 if older else 120
        if older and len(self.chat.messages) + len(pairs) > cap:
            self.status("此窗口最多显示 200 条消息。按 F5 回到最新。")
            self.query_one("#more-turns", Button).disabled = True
            return
        if chat.message_revision != original_revision:
            return
        if automatic and not changed:
            if incremental:
                prefix = self.chat.messages[:-self.chat.tail_size] if self.chat.tail_size else self.chat.messages
                pairs = prefix + pairs
            await self.sync_transcript(pairs[-200:])
            chat.tail_id = turns[-1]["id"] if turns else None
            chat.tail_size = min(len(entries[-1]), len(chat.messages)) if entries else 0
            if self.chat is chat:
                self.update_usage()
            return
        messages = (pairs + chat.messages) if older else pairs[-cap:]
        needs_paint = messages != chat.messages
        chat.messages = messages
        chat.loaded = True
        if not older:
            self.chat.tail_id = turns[-1]["id"] if turns else None
            self.chat.tail_size = min(len(entries[-1]), len(self.chat.messages)) if entries else 0
        if needs_paint:
            await self.paint_history(chat, older=older)
        if self.chat is not chat:
            return
        self.update_usage()
        self.status(self.project or "本地 Codex 会话")
        if self.backend.shared_url and not older:
            try:
                await self.subscribe_chat(chat)
            except (CodexError, OSError) as error:
                if self.chat is chat:
                    self.status(f"共享会话订阅失败：{error}")

    async def subscribe_chat(self, chat, *, for_send=False):
        thread_id = chat.current["id"]
        async with self.subscription_lock:
            if thread_id in self.subscriptions and (not for_send or chat.asking_enabled):
                return
            params = {"threadId": thread_id, "excludeTurns": True}
            if for_send:
                params["config"] = QUESTION_CONFIG
            resumed = await self.backend.request("thread/resume", params)
            chat.server_model = resumed.get("model") or resumed["thread"].get("model") or chat.server_model
            chat.server_effort = resumed.get("reasoningEffort") or resumed["thread"].get("reasoningEffort") or chat.server_effort
            self.subscriptions.add(thread_id)
            chat.asking_enabled |= for_send
        if (resumed["thread"].get("status") or {}).get("type") == "active" and not chat.active_turn:
            page = await self.backend.request("thread/turns/list", {
                "threadId": thread_id, "limit": 1, "itemsView": "summary", "sortDirection": "desc",
            })
            latest = next(iter(page["data"]), None)
            if latest and latest["status"] == "inProgress" and not chat.active_turn:
                self.mark_turn_started(chat, latest["id"])

    def mark_turn_started(self, chat, turn_id):
        chat.busy = True
        chat.awaiting_turn = False
        chat.active_turn = turn_id
        if self.chat is chat:
            self.update_send_controls()
            self.status("正在压缩上下文…" if chat.compacting else "Codex 正在回复…")
        self.draw_tree(preserve_scroll=True)

    async def sync_transcript(self, recent):
        chat = self.chat
        async with self.history_lock:
            if self.chat is not chat or (recent and chat.messages[-len(recent):] == recent):
                return
            if not recent and not chat.messages:
                return
            log = self.query_one("#history", VerticalScroll)
            follow = log.is_vertical_scroll_end
            position = log.scroll_y
            old = chat.messages
            overlap = min(len(old), len(recent))
            while overlap and old[-overlap:] != recent[:overlap]:
                overlap -= 1
            if overlap:
                extra = recent[overlap:]
                overflow = max(0, len(old) + len(extra) - 200)
                chat.messages = (old + extra)[-200:]
                if overflow:
                    await log.remove_children(list(log.children)[:overflow])
                await log.mount_all(self.transcript_widget(role, body) for role, body in extra)
            else:
                expanded = {child.detail for child in log.children if isinstance(child, ToolBlock) and not child.collapsed}
                chat.messages = recent
                chat.images = []
                widgets = [self.transcript_widget(role, body) for role, body in recent]
                await log.remove_children()
                await log.mount_all(widgets)
                for widget in widgets:
                    if isinstance(widget, ToolBlock) and widget.detail in expanded:
                        widget.toggle()
            if self.chat is chat:
                if follow:
                    self.call_after_refresh(log.scroll_end, animate=False)
                else:
                    self.call_after_refresh(log.scroll_to, y=position, animate=False)

    def item_text(self, item):
        kind = item.get("type")
        if kind == "agentMessage":
            return "Codex", item.get("text", "")[-14000:]
        if kind == "userMessage":
            parts = [submitted_text(x.get("text") or "") if x.get("type") == "text" else x.get("path") or "[附件]"
                     for x in item.get("content", [])]
            return "你", "\n".join(p for p in parts if p)[-14000:]
        if kind == "imageGeneration":
            path = item.get("savedPath")
            return ("Codex", f"[生成的图片](<{path}>)") if path else None
        detail = tool_detail(item)
        return ("工具", detail) if detail else None

    def transcript_widget(self, role, body):
        if role == "Codex":
            for match in IMAGE_LINK.finditer(body):
                path = match.group(1)
                if not path.startswith(("https:", "http:")) and path not in self.chat.images:
                    self.chat.images.append(path)
                    self.chat.images = self.chat.images[-30:]
        return message_block(role, body)

    async def record_message(self, role, body, *, chat=None):
        chat = chat or self.chat
        async with self.history_lock:
            chat.messages.append((role, body))
            chat.message_revision += 1
            overflow = len(chat.messages) > 200
            if overflow:
                chat.messages.pop(0)
            if self.chat is not chat:
                return
            log = self.query_one("#history", VerticalScroll)
            widget = self.transcript_widget(role, body)
            if overflow and log.children:
                await log.children[0].remove()
            await log.mount(widget)

    def render_live(self):
        self.btw.render_live()
        if self.live_dirty:
            self.live_dirty = False
            widget = next(self.query("#live").results(Static), None)
            if widget is None:
                return
            preview = "\n".join(self.chat.live.values())[-2300:]
            widget.display = bool(preview)
            if preview:
                widget.update(markdown(preview))

    def update_usage(self):
        fields = self.cache_lines(self.chat) + self.quota_lines()
        if self.chat.permission:
            fields.append("只读" if self.chat.permission == "readOnly" else "工作区写入")
        self.query_one("#usage-line", Label).update(Text("  │  ".join(fields)))
        model, info = self.effective_model()
        effort = self.effective_effort(self.chat, info)
        self.query_one("#model-picker", Button).label = Text((info.get("displayName") or model or "模型")[:24] + " ▾")
        self.query_one("#effort-picker", Button).label = Text((effort or "思考强度") + " ▾")

    def cache_lines(self, chat):
        fields = []
        for key, name in (("last", "本次缓存"), ("total", "会话缓存")):
            tokens = chat.usage.get(key) or {}
            count = tokens.get("inputTokens")
            cached = tokens.get("cachedInputTokens")
            if count and cached is not None:
                fields.append(f"{name} {cached / count:.1%}")
        return fields

    def quota_lines(self):
        fields = []
        for key in ("primary", "secondary"):
            window = self.quota.get(key)
            if window and window.get("usedPercent") is not None:
                minutes = window.get("windowDurationMins") or 0
                label = (f"{minutes // 60}h" if minutes >= 60 and minutes % 60 == 0 else f"{minutes}m") if minutes else ""
                reset = window.get("resetsAt")
                when = f" · {datetime.fromtimestamp(reset):%m-%d %H:%M}重置" if reset else ""
                fields.append(f"{label}额度已用 {window['usedPercent']}%{when}")
        return fields

    def model_info(self, model=None):
        if model:
            return next((m for m in self.models if m["model"] == model), {})
        return next((m for m in self.models if m.get("isDefault")), {})

    def effective_model(self, chat=None):
        chat = chat or self.chat
        current = chat.model or chat.server_model or (chat.current or {}).get("model")
        info = self.model_info(current)
        return current or info.get("model"), info

    def effective_effort(self, chat, info):
        saved = (chat.server_effort or (chat.current or {}).get("reasoningEffort")) if not chat.model else None
        return chat.effort or saved or info.get("defaultReasoningEffort")

    def render_attachments(self):
        widget = self.query_one("#attachments", Label)
        label = Text(f"附件 {len(self.chat.attachments)}  ")
        for index, path in enumerate(map(Path, self.chat.attachments)):
            if index:
                label.append(" · ")
            label.append(path.name, style="bold #ffd866" if path.suffix.lower() in IMAGE_TYPES else None)
        widget.update(label)
        widget.display = bool(self.chat.attachments)
        self.query_one("#remove-attachment", Button).disabled = not self.chat.attachments
        self.query_one("#clear-attachments", Button).disabled = not self.chat.attachments

    def update_send_controls(self):
        self.query_one("#send", Button).disabled = (
            not self.connected or not self.chat.current or self.chat.busy or self.loading_history or self.connecting
        )
        self.query_one("#stop", Button).disabled = not self.connected or not self.chat.active_turn
        button = self.query_one("#pending-request", Button)
        button.display = self.connected and bool(self.chat.requests)
        if button.display:
            first = next(iter(self.chat.requests.values()))
            button.label = "回答问题" if first["method"] == "item/tool/requestUserInput" else "处理审批"

    def action_sidebar(self):
        sidebar = self.query_one("#sidebar")
        sidebar.display = not sidebar.display

    def action_search(self):
        self.query_one("#sidebar").display = True
        self.query_one("#search", Input).focus()

    @work(group="automatic-sync")
    async def sync_local(self):
        if self.syncing or self.closing:
            return
        if not self.connected:
            if self.backend.shared_mode and not self.connecting and time.monotonic() >= self.reconnect_at:
                self.connect()
            return
        self.syncing = True
        try:
            now = time.monotonic()
            page = await self.backend.request("thread/list", THREAD_LIST_OPTIONS)
            marker = [(r["id"], r.get("name"), r.get("recencyAt"), r.get("projectId")) for r in page["data"]]
            prefs = await asyncio.to_thread(file_stamp, self.desktop_state_path)
            if marker != self.list_stamp or prefs != self.sidebar_stamp or now - self.last_full_poll >= 15:
                await self.load_threads(refresh_projects=prefs != self.sidebar_stamp, automatic=True).wait()
                self.last_full_poll = now
                self.list_stamp = marker
            await self.sync_current()
        except (CodexError, OSError, WorkerCancelled):
            pass  # F5 or reopening the conversation still reports specific errors.
        finally:
            self.syncing = False

    async def sync_current(self):
        thread = self.chat.current
        if self.chat.busy or self.loading_history or not thread or not thread["id"]:
            return
        tid = thread["id"]
        stamp = await asyncio.to_thread(file_stamp, thread.get("path"))
        if thread.get("historyMode") == "legacy":
            latest = (await self.backend.request("thread/read", {"threadId": tid}))["thread"]
            marker = (latest.get("updatedAt"), latest.get("recencyAt"))
        else:
            response = await self.backend.request("thread/turns/list", {
                "threadId": tid, "limit": 1, "itemsView": "summary", "sortDirection": "desc",
            })
            marker = tuple((t["id"], t["status"], t.get("completedAt"), len(t["items"])) for t in response["data"])
        if self.chat.current is not thread or self.chat.busy or self.loading_history:
            return
        if marker != self.chat.turn_stamp or stamp != self.chat.rollout_stamp:
            await self.open_thread(tid, automatic=True).wait()
            if self.chat.current and self.chat.current["id"] == tid:
                self.chat.turn_stamp, self.chat.rollout_stamp = marker, stamp

    def action_refresh(self):
        if not self.connected:
            if not self.connecting:
                self.connect()
            return
        self.load_threads(refresh_projects=True)
        if self.chat.current and self.chat.current["id"] and not self.chat.busy:
            self.open_thread(self.chat.current["id"])

    async def action_close_client(self):
        if not self.backend.shared_url and any(
                chat.busy for chat in [self.chat, *self.chats.values(), *self.drafts.values(), *self.btw.chats.values()]):
            self.status("请先停止或等待当前回复，再退出。")
            return
        self.closing = True
        try:
            state = {"expanded": sorted(self.expanded), "sidebar_hidden": not self.query_one("#sidebar").display}
            tmp = STATE_PATH.with_suffix(".tmp")
            tmp.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
            tmp.replace(STATE_PATH)
        except OSError as error:
            self.status(error)
        self.exit()

    async def on_unmount(self):
        self.closing = True
        await self.backend.close()

    @work
    async def action_settings(self):
        chat = self.chat
        selected = await self.push_screen_wait(Settings(chat.permission))
        if selected is not None:
            chat.permission = selected
            if self.chat is chat:
                self.update_usage()

    @work(exclusive=True, group="model-picker")
    async def choose_model_setting(self, kind, *, side=None):
        if not self.connected:
            return
        target = side or self.chat
        if not self.models:
            try:
                self.status("正在读取账号可用模型…")
                await self.load_model_catalog()
                self.status(self.project or "已读取可用模型。")
            except (CodexError, OSError) as error:
                self.status(error)
                return
        visible_target = self.btw.current if side else self.chat
        if target is not visible_target or self.screen is not self.screen_stack[0]:
            return
        if side:
            inherited, inherited_info = self.effective_model()
            effective = target.model or inherited
            info = self.model_info(effective) if target.model else inherited_info
        else:
            effective, info = self.effective_model(target)
            inherited = target.server_model or (target.current or {}).get("model")
        caption = "BTW · " if side else ""
        if kind == "model":
            choices = [("沿用主对话" if side else "沿用会话 / 默认模型", "")]
            choices.extend((m.get("displayName") or m["model"], m["model"]) for m in self.models)
            selected = await self.push_screen_wait(Picker(caption + "选择模型", choices, target.model if side else target.model or effective))
            if selected is None:
                return
            target.model = selected
            info = self.model_info(selected or inherited)
            supported = {e["reasoningEffort"] for e in info.get("supportedReasoningEfforts", [])}
            if target.effort not in supported:
                target.effort = ""
        else:
            choices = [("沿用主对话 / 所选模型默认" if side else "沿用会话 / 模型默认", "")]
            choices.extend((e["reasoningEffort"], e["reasoningEffort"]) for e in info.get("supportedReasoningEfforts", []))
            selected = await self.push_screen_wait(Picker(caption + "选择思考强度", choices, target.effort))
            if selected is None:
                return
            target.effort = selected
        if side:
            self.btw.save_settings(target)
        else:
            self.update_usage()

    async def load_model_catalog(self):
        self.models = (await self.backend.request("model/list", {"limit": 80}))["data"]

    async def read_quota(self):
        response = await self.backend.request("account/rateLimits/read")
        self.quota = response.get("rateLimits") or {}
        self.update_usage()

    @work
    async def refresh_quota(self):
        if not self.connected:
            return
        try:
            await self.read_quota()
            self.status("已刷新额度。" if self.quota else "账号未返回额度数据。")
        except (CodexError, OSError) as error:
            self.status(error)

    def status_detail(self, chat, quota_state=""):
        connection = "未连接"
        if self.connected:
            connection = "共享后端" if self.backend.shared_url else "本机独立后端"
        elif self.connecting:
            connection = "连接中"
        model, info = self.effective_model(chat)
        title = (chat.current or {}).get("name") or (chat.current or {}).get("preview")
        title = clean_label(title) if title else "未选择聊天" if not chat.current else "未命名聊天"
        fields = [f"连接 · {connection}", f"聊天 · {title}",
                  f"模型 · {info.get('displayName') or model or '沿用 Codex 设置'}",
                  f"思考强度 · {self.effective_effort(chat, info) or '沿用 Codex 设置'}"]
        fields.extend(self.cache_lines(chat) or ["缓存命中率 · 暂无数据"])
        limits = self.quota_lines()
        if limits and (quota_state or not self.connected):
            fields.append("额度 · 上次读取值")
        fields.extend(limits or ["账号额度 · 暂无数据"])
        if quota_state:
            fields.append(quota_state)
        return "\n".join(fields)

    @work(exclusive=True, group="status")
    async def show_status(self):
        if self.screen is not self.screen_stack[0]:
            return
        chat = self.chat
        refresh = self.connected
        dialog = StatusDialog(self.status_detail(chat, "正在刷新账号额度…" if refresh else ""))
        await self.push_screen(dialog)
        if refresh:
            detail = ""
            try:
                await self.read_quota()
            except (CodexError, OSError) as error:
                detail = f"额度刷新失败：{error}"
            if self.screen is dialog:
                dialog.update_detail(self.status_detail(chat, detail))

    @work
    async def file_action(self, mode):
        chat = self.chat
        path = await self.push_screen_wait(PathDialog(self.project or Path.cwd(), self.chat.images, mode))
        if not path:
            return
        if mode == "attach":
            if path not in chat.attachments:
                chat.attachments.append(path)
            if self.chat is chat:
                self.render_attachments()
        elif mode == "image":
            try:
                os.startfile(path)
            except OSError as error:
                self.status(error)
        elif self.connected:
            project = next((p for p in self.projects if any(Path(root["path"]) == Path(path)
                           for root in p.get("roots", []) if root.get("path"))), None)
            await self.new_draft(path, project["id"] if project else None)

    @work
    async def paste_clipboard(self, allow_text=False):
        if self.pasting:
            return
        if self.chat.current is None:
            self.status("先选择一条聊天或新建聊天。")
            return
        target = self.chat
        self.pasting = True
        try:
            path, text = await read_clipboard(allow_text)
            if self.chat is not target:
                if path:
                    target.attachments.append(str(path))
                elif text:
                    target.draft += text.replace("\r\n", "\n")
                return
            prompt = self.query_one("#prompt", Composer)
            if path:
                self.chat.attachments.append(str(path))
                self.render_attachments()
                self.status("已粘贴图片。" if self.connected else "图片已加入草稿 · 后端已断开，F5 重连")
            elif text:
                prompt.insert_paste(text.replace("\r\n", "\n"))
            else:
                self.status("剪贴板中没有可粘贴的图片。")
            prompt.focus()
        except OSError as error:
            self.status(error)
        finally:
            self.pasting = False

    async def new_draft(self, path, project_id=None):
        self.workers.cancel_group(self, "history")
        self.expanded.add(path)
        key = project_id, path
        chat = self.drafts.get(key)
        if chat is None:
            chat = self.drafts[key] = ChatState(current={"id": None, "cwd": path}, project_id=project_id, loaded=True)
        await self.activate_chat(chat)
        self.query_one("#prompt", TextArea).focus()
        self.status(path)

    @work(exclusive=True, group="create-project")
    async def create_project(self):
        if not self.connected:
            self.status("先连接 Codex 后端，再新建项目。")
            return
        selection = await self.push_screen_wait(ProjectDialog(self.project or Path.cwd()))
        if selection is None:
            return
        name, path = selection
        key = self.project_creation_keys.setdefault(selection, str(uuid4()))
        try:
            result = await self.backend.request("project/create", {
                "name": name, "roots": [{"path": path}], "idempotencyKey": key,
            })
            project_id = result["project"]["id"]
        except (CodexError, OSError) as error:
            self.status(f"项目创建未确认：{error}")
            return
        self.expanded.add(project_id)
        self.load_threads(refresh_projects=True)
        await self.new_draft(path, project_id)
        self.status(f"项目已创建 · {name}")

    @on(Button.Pressed)
    def button(self, event):
        clicked = event.button.id
        if clicked == "sidebar-toggle":
            self.action_sidebar()
        elif clicked == "btw-toggle":
            self.set_btw_visible(not self.btw.display)
        elif clicked == "refresh":
            self.action_refresh()
        elif clicked == "more-turns" and self.chat.current:
            self.open_thread(self.chat.current["id"], older=True)
        elif clicked == "quota":
            self.refresh_quota()
        elif clicked == "pending-request":
            self.show_pending_request(self.chat)
        elif clicked == "new-project":
            self.create_project()
        elif clicked == "settings":
            self.action_settings()
        elif clicked in {"model-picker", "effort-picker"}:
            self.choose_model_setting(clicked.removesuffix("-picker"))
        elif clicked == "chatgpt":
            webbrowser.open("https://chatgpt.com/")
        elif clicked == "desktop":
            self.open_desktop()
        elif clicked in {"attach", "image", "new-chat"}:
            self.file_action("project" if clicked == "new-chat" else clicked)
        elif clicked == "paste-image":
            self.paste_clipboard()
        elif clicked == "clear-attachments":
            self.chat.attachments.clear()
            self.render_attachments()
        elif clicked == "remove-attachment":
            self.remove_attachment()
        elif clicked == "send":
            self.action_send_prompt()
        elif clicked == "stop":
            self.stop_turn(self.chat)

    @work
    async def remove_attachment(self):
        paths = self.chat.attachments.copy()
        current = self.chat.current
        if not paths:
            return
        selection = await self.push_screen_wait(Picker(
            "移除一项待发送附件",
            [(f"{index + 1}. {Path(path).name}", index) for index, path in enumerate(paths)],
            len(paths) - 1,
        ))
        if selection is not None and self.chat.current is current and self.chat.attachments == paths:
            self.chat.attachments.pop(selection)
            self.render_attachments()

    def open_desktop(self):
        if self.chat.busy and not self.backend.shared_url:
            self.status("等当前回复结束后再切回桌面。")
            return
        tid = (self.chat.current or {}).get("id")
        if not tid:
            self.status("选中一条已保存的聊天后，可以在桌面端打开它。")
            return
        try:
            thread = str(UUID(tid))
            if self.backend.shared_url:
                from shared_state import read_endpoint
                if read_endpoint() != self.backend.shared_url:
                    self.status("当前地址由外部启动器管理。请从同一启动器打开桌面。")
                    return
                subprocess.Popen(
                    [sys.executable, str(Path(__file__).with_name("shared_owner.py")), "--open-desktop", "--thread", thread],
                    creationflags=subprocess.CREATE_NO_WINDOW,
                )
            else:
                os.startfile(f"codex://threads/{thread}")
        except (ValueError, OSError) as error:
            self.status(error)

    def set_btw_visible(self, visible):
        if visible:
            self.btw.open()
        else:
            self.btw.display = False
            self.query_one("#prompt", TextArea).focus()
        self.query_one("#btw-toggle", Button).label = "BTW ◂" if visible else "BTW ▸"

    @on(TextArea.Changed, "#prompt")
    def update_slash_menu(self, event):
        menu = self.query_one("#slash-commands", OptionList)
        text = event.text_area.text
        choices = [Option(f"{key}   {label}", id=key) for key, label in SLASH_COMMANDS.items()
                   if text.startswith("/") and key.startswith(text)]
        menu.clear_options().add_options(choices)
        menu.display = bool(choices)
        menu.highlighted = 0 if choices else None

    def slash_menu_key(self, key):
        menu = self.query_one("#slash-commands", OptionList)
        if not menu.display:
            return False
        if key == "up":
            menu.action_cursor_up()
        elif key == "down":
            menu.action_cursor_down()
        elif key == "escape":
            menu.display = False
        else:
            return False
        return True

    @on(OptionList.OptionSelected, "#slash-commands")
    def slash_selected(self, event):
        self.run_slash_command(event.option.id)

    def run_slash_command(self, command, argument=""):
        prompt = self.query_one("#prompt", TextArea)
        if command == "/btw":
            self.set_btw_visible(True)
            if argument and not self.btw.open(argument):
                self.status("右栏暂不能发送，命令输入已保留。请检查连接、主聊天或右栏草稿。")
                return
        elif command == "/compact":
            self.compact_chat(self.chat)
            prompt.focus()
        elif command == "/status":
            self.show_status()
            prompt.focus()
        prompt.clear()
        self.query_one("#slash-commands").display = False

    def compact_feedback(self, chat, message):
        chat.compact_status = message
        if self.chat is chat:
            self.status(message)

    @work
    async def compact_chat(self, chat):
        tid = (chat.current or {}).get("id")
        if not tid or not self.connected or self.connecting:
            self.compact_feedback(chat, "请先连接后端并打开一条已保存的聊天。")
            return
        if chat.busy:
            self.compact_feedback(chat, "这条聊天正在运行，结束后可压缩。")
            return
        chat.busy = chat.awaiting_turn = True
        if self.chat is chat:
            self.update_send_controls()
        try:
            fresh = await self.backend.request("thread/read", {"threadId": tid})
            if (fresh["thread"].get("status") or {}).get("type") == "active":
                self.reset_turn(chat)
                self.compact_feedback(chat, "这条聊天正在运行，结束后可压缩。")
                return
            await self.subscribe_chat(chat)
            if chat.active_turn:
                self.compact_feedback(chat, "这条聊天正在运行，结束后可压缩。")
                return
            chat.compacting = True
            self.compact_feedback(chat, "正在压缩上下文…")
            await self.backend.request("thread/compact/start", {"threadId": tid})
        except (CodexError, OSError) as error:
            if not chat.active_turn:
                self.reset_turn(chat)
            self.compact_feedback(chat, f"压缩未确认：{error}" if isinstance(error, ConnectionInterrupted)
                                  else f"压缩失败：{error}")
            self.detach(tid)

    @work
    async def action_send_prompt(self):
        chat = self.chat
        prompt = self.query_one("#prompt", TextArea)
        parts = prompt.text.split(maxsplit=1)
        if prompt.text.startswith("/") and "\n" not in prompt.text and parts and (
                parts[0] == "/btw" or (len(parts) == 1 and parts[0] in SLASH_COMMANDS)):
            self.run_slash_command(parts[0], parts[1] if len(parts) > 1 else "")
            return
        menu = self.query_one("#slash-commands", OptionList)
        if menu.display and menu.highlighted_option and menu.highlighted_option.id.startswith(prompt.text):
            self.run_slash_command(menu.highlighted_option.id)
            return
        if not chat.current:
            self.status("先选择一条聊天或新建聊天。")
            return
        if self.pasting:
            self.status("正在粘贴图片，请稍等。")
            return
        if not self.connected or self.connecting:
            self.status("后端未连接，草稿已保留。按 F5 重连。")
            return
        if chat.busy:
            self.status("这条聊天正在运行，草稿已保留。")
            return
        while self.loading_history and self.chat is chat:
            await asyncio.sleep(0.025)
        if not self.connected or self.chat is not chat or chat.busy:
            return
        prompt = self.query_one("#prompt", TextArea)
        original = prompt.text
        text = original.strip()
        files = list(chat.attachments)
        if not text and not files:
            return
        chat.compact_status = ""
        chat.busy = chat.awaiting_turn = True
        self.update_send_controls()
        self.status("正在向 Codex 发送…")
        tid = chat.current["id"]
        submitting = False
        params = {"input": []}
        if chat.model:
            params["model"] = chat.model
        if chat.effort:
            params["effort"] = chat.effort
        if chat.permission:
            policy = {"type": chat.permission, "networkAccess": False}
            if chat.permission == "workspaceWrite":
                policy["writableRoots"] = [chat.current["cwd"]]
            params.update(sandboxPolicy=policy, approvalPolicy="on-request")
        references = [file for file in files if Path(file).suffix.lower() not in IMAGE_TYPES]
        if references:
            text += "\n\n本地文件路径（在当前权限允许时读取）：\n" + "\n".join(references)
        if text.strip():
            params["input"].append({"type": "text", "text": text.strip()})
        params["input"].extend({"type": "localImage", "path": file} for file in files if file not in references)
        try:
            if tid:
                fresh = await self.backend.request("thread/read", {"threadId": tid})
                if (fresh["thread"].get("status") or {}).get("type") == "active":
                    self.reset_turn(chat)
                    await self.subscribe_chat(chat)
                    if self.chat is chat:
                        self.status("这条聊天正在运行。完成后再发送。")
                    return
                await self.subscribe_chat(chat, for_send=True)
                if chat.active_turn:
                    if self.chat is chat:
                        self.status("这条聊天正在运行。完成后再发送。")
                    return
            else:
                start = {"cwd": chat.current["cwd"], "ephemeral": False, "config": QUESTION_CONFIG}
                if chat.project_id:
                    start["projectId"] = chat.project_id
                started = await self.backend.request("thread/start", start)
                chat.current = started["thread"]
                tid = chat.current["id"]
                if self.chat is chat:
                    self.btw.set_parent(tid)
                self.chats[tid] = chat
                self.subscriptions.add(tid)
                chat.asking_enabled = True
                self.drafts = {key: value for key, value in self.drafts.items() if value is not chat}
            submitting = True
            result = await self.backend.request("turn/start", {**params, "threadId": tid})
            if params.get("model") and params["model"] != chat.server_model:
                chat.server_model = params["model"]
                chat.server_effort = ""
            chat.server_effort = params.get("effort") or chat.server_effort
            if chat.awaiting_turn and result.get("turn"):
                self.mark_turn_started(chat, result["turn"]["id"])
        except (CodexError, OSError) as error:
            if self.chat is chat:
                if submitting and isinstance(error, (ConnectionInterrupted, OSError)):
                    self.status("发送结果未确认，草稿已保留。重连后先核对聊天记录。")
                elif "active writer" in str(error).lower():
                    self.status("桌面端或另一终端占用此聊天。草稿已保留。")
                else:
                    self.status(error)
            if not chat.active_turn:
                self.reset_turn(chat)
            if tid and not self.backend.shared_url:
                self.detach(tid)
        else:
            if self.chat is chat:
                if prompt.text == original:
                    prompt.clear()
            elif chat.draft == original:
                chat.draft = ""
            chat.attachments[:] = [file for file in chat.attachments if file not in files]
            if self.chat is chat:
                self.render_attachments()
            self.load_threads(automatic=True)

    @work
    async def stop_turn(self, chat=None):
        chat = chat or self.chat
        if chat.active_turn and chat.current:
            try:
                await self.backend.request("turn/interrupt", {
                    "threadId": chat.current["id"], "turnId": chat.active_turn,
                })
            except (CodexError, OSError) as error:
                self.status(error)

    def reset_turn(self, chat):
        chat.busy = chat.awaiting_turn = False
        chat.compacting = False
        chat.active_turn = None
        chat.file_changes.clear()
        chat.live.clear()
        chat.tail_id = None
        if self.chat is chat:
            self.live_dirty = True
            self.update_send_controls()

    @work
    async def detach(self, tid):
        async with self.subscription_lock:
            chat = self.chats.get(tid)
            if (tid not in self.subscriptions or not chat or chat.busy or chat.requests
                    or (self.chat is chat and self.backend.shared_url)):
                return
            try:
                await self.backend.request("thread/unsubscribe", {"threadId": tid})
                self.subscriptions.discard(tid)
                chat.asking_enabled = False
            except (CodexError, OSError):
                pass

    @work
    async def restore_subscriptions(self):
        for tid in list(self.reconnect_threads):
            if not self.connected:
                break
            chat = self.chats.get(tid)
            if chat:
                try:
                    await self.subscribe_chat(chat)
                except (CodexError, OSError):
                    continue
            self.reconnect_threads.discard(tid)

    def requests_changed(self, chat):
        if self.chat is chat:
            self.update_send_controls()
        self.draw_tree(preserve_scroll=True)

    async def backend_event(self, event):
        if self.closing:
            return
        method, params = event["method"], event.get("params") or {}
        tid = params.get("threadId")
        if tid in self.btw.threads:
            await self.btw.backend_event(tid, event)
            return
        chat = self.chats.get(tid)
        if "id" in event:
            if method not in {"item/commandExecution/requestApproval", "item/fileChange/requestApproval",
                              "item/permissions/requestApproval", "item/tool/requestUserInput"} or not tid:
                try:
                    await self.backend.send({"id": event["id"], "error": {
                        "code": -32601, "message": "Unsupported client request",
                    }})
                except (CodexError, OSError):
                    pass
                return
            if chat is None:
                row = next((row for row in self.rows if row["id"] == tid), {"id": tid})
                chat = self.chats.setdefault(tid, ChatState(current=dict(row)))
            chat.requests[event["id"]] = event
            self.requests_changed(chat)
            if self.chat is chat:
                self.show_pending_request(chat)
            return
        if method == "connection/closed":
            self.connected = False
            self.btw.disconnected()
            self.query_one("#title", Label).tooltip = None
            self.subscriptions.clear()
            for tid, state in self.chats.items():
                if state.busy or state.requests:
                    self.reconnect_threads.add(tid)
                state.requests.clear()
                state.asking_enabled = False
                if state.compacting:
                    state.compact_status = "压缩结果未确认 · 连接已断开"
                self.reset_turn(state)
            self.dismiss_request_dialog()
            self.requests_changed(self.chat)
            self.reconnect_at = time.monotonic() + 2
            self.status(f"后端已断开，草稿已保留 · F5 重连 · {params['message']}")
        elif method == "account/rateLimits/updated":
            self.quota = params.get("rateLimits") or {}
            self.update_usage()
        elif chat is not None:
            current = self.chat is chat
            if method == "serverRequest/resolved":
                self.resolve_request(chat, params.get("requestId"))
            elif method == "thread/name/updated":
                name = params.get("threadName") or params.get("name")
                if name:
                    chat.current["name"] = name
                    if current:
                        self.query_one("#thread-title", Label).update(Text(clean_label(name)))
            elif method == "thread/tokenUsage/updated":
                chat.usage = params["tokenUsage"]
                if current:
                    self.update_usage()
            elif method == "turn/started":
                self.mark_turn_started(chat, params["turn"]["id"])
            elif method == "item/agentMessage/delta":
                item_id = params["itemId"]
                chat.live[item_id] = (chat.live.get(item_id, "") + params["delta"])[-2300:]
                if current:
                    self.live_dirty = True
            elif method == "item/started":
                item = params["item"]
                if item["type"] == "fileChange":
                    chat.file_changes[item["id"]] = item.get("changes", [])
            elif method == "item/completed":
                item = params["item"]
                chat.live.pop(item["id"], None)
                if current:
                    self.live_dirty = True
                if entry := self.item_text(item):
                    await self.record_message(*entry, chat=chat)
            elif method == "turn/completed":
                result = params["turn"]
                if chat.active_turn and chat.active_turn != result["id"]:
                    return
                if current:
                    error = result.get("error") or {}
                    if not chat.compacting:
                        self.status(error.get("message") or {
                            "completed": "回复完成", "interrupted": "已停止", "failed": "回复失败",
                        }.get(result["status"], result["status"]))
                if chat.compacting:
                    status = result["status"]
                    detail = (result.get("error") or {}).get("message") or status
                    self.compact_feedback(chat, "上下文压缩完成" if status == "completed" else f"压缩未完成：{detail}")
                for request_id, request in list(chat.requests.items()):
                    if (request.get("params") or {}).get("turnId") == result["id"]:
                        self.resolve_request(chat, request_id, changed=False)
                self.reset_turn(chat)
                self.requests_changed(chat)
                self.load_threads(automatic=True)
                self.detach(tid)

    def dismiss_request_dialog(self, chat=None, request_id=None):
        if self.request_dialog:
            target, identifier, screen = self.request_dialog
            if (chat is None or (chat is target and request_id == identifier)) and self.screen is screen:
                screen.dismiss(None)

    def resolve_request(self, chat, request_id, *, changed=True):
        if chat.requests.pop(request_id, None) is not None:
            self.dismiss_request_dialog(chat, request_id)
            if changed:
                self.requests_changed(chat)

    @work(group="approval")
    async def show_pending_request(self, chat=None):
        chat = chat or self.chat
        if (self.chat is not chat or not self.connected or not chat.requests
                or self.request_dialog or self.screen is not self.screen_stack[0]):
            return
        request_id, event = next(iter(chat.requests.items()))
        method, params = event["method"], event.get("params") or {}
        if method == "item/tool/requestUserInput":
            if not params.get("questions"):
                return
            screen = Questions(params["questions"])
        else:
            if method == "item/commandExecution/requestApproval":
                network = params.get("networkApprovalContext")
                detail = params.get("command") or (json.dumps(network, ensure_ascii=False) if network else "")
                title = "Codex 请求执行审批"
            elif method == "item/fileChange/requestApproval":
                changes = chat.file_changes.get(params.get("itemId"), [])
                detail = "\n".join(f"{item.get('path', '')}\n{item.get('diff', '')}" for item in changes)
                title = "Codex 请求文件修改审批"
            else:
                detail = json.dumps(params.get("permissions") or {}, ensure_ascii=False, indent=2)
                title = "Codex 请求本轮附加权限"
            content = "\n".join(filter(None, [params.get("reason"), params.get("cwd"), detail]))
            screen = Approval(title, content, bool(detail and detail != "{}"), defer=True)
        self.request_dialog = (chat, request_id, screen)
        try:
            decision = await self.push_screen_wait(screen)
            if decision is None or decision == "defer" or chat.requests.get(request_id) is not event:
                return
            if method == "item/tool/requestUserInput":
                result = {"answers": decision}
            elif method == "item/permissions/requestApproval":
                granted = (params.get("permissions") or {}) if decision == "accept" else {}
                result = {"permissions": granted, "scope": "turn"}
            else:
                result = {"decision": "accept" if decision == "accept" and screen.can_accept else "decline"}
            await self.backend.send({"id": request_id, "result": result})
            self.resolve_request(chat, request_id)
        except (CodexError, OSError) as error:
            self.status(error)
        finally:
            self.request_dialog = None


def main():
    parser = argparse.ArgumentParser(description="Codex Light TUI")
    parser.add_argument("--handoff-thread")
    parser.add_argument("--handoff-ready", type=Path)
    args = parser.parse_args()
    if (args.handoff_thread is None) != (args.handoff_ready is None):
        parser.error("--handoff-thread and --handoff-ready must be supplied together")
    app = CodexLight(handoff_thread=args.handoff_thread, handoff_ready=args.handoff_ready)
    app.run()
    return app.return_code


if __name__ == "__main__":
    sys.exit(main())
