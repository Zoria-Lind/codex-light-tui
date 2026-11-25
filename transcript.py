"""Compact, selectable terminal presentation of Codex chat items."""

from dataclasses import dataclass
import json
from pathlib import PureWindowsPath
import re

from markdown_it import MarkdownIt
from markdown_it.token import Token
from mdit_py_plugins.dollarmath import dollarmath_plugin
from mdit_py_plugins.texmath import texmath_plugin
from rich.cells import cell_len
from rich.text import Text
from rich.console import Group
from rich.markdown import Markdown
from textual import on
from textual.binding import Binding
from textual.content import Content
from textual.containers import Vertical, VerticalScroll
from textual.widgets import Button, Static


DETAIL_LIMIT = 8_000
WRAPPED_SHELL = re.compile(r'^"[^"\r\n]+"\s+-(?:Command|c)\s+', re.I)


class TranscriptPane(Vertical):
    def __init__(self, history_id):
        super().__init__(classes="transcript-pane")
        self.history = VerticalScroll(id=history_id)
        self.latest = Button("↓", id=f"{history_id}-latest", classes="transcript-latest",
                             tooltip="回到最新消息")

    def compose(self):
        yield self.history
        yield self.latest

    def on_mount(self):
        self.history.anchor()
        self.watch(self.history, "scroll_y", self.update_latest, init=False)
        self.watch(self.history, "virtual_size", self.schedule_latest, init=False)

    def on_resize(self):
        self.latest.styles.offset = (max(0, self.size.width - 8), -1)
        self.schedule_latest()

    def schedule_latest(self):
        self.call_after_refresh(self.update_latest)

    def update_latest(self):
        self.latest.display = not self.history.is_vertical_scroll_end

    @on(Button.Pressed, ".transcript-latest")
    def jump_latest(self, event):
        event.stop()
        self.history.scroll_end(animate=False, immediate=True, x_axis=False)
        self.update_latest()


def limited(text, limit=DETAIL_LIMIT):
    if len(text) <= limit:
        return text
    head = limit * 3 // 4
    return f"{text[:head]}\n… 省略 {len(text) - limit:,} 个字符 …\n{text[-(limit - head):]}"


def submitted_text(text):
    """Hide the desktop attachment wrapper in the displayed transcript only."""
    if text.startswith("# Files mentioned by the user:"):
        header, separator, request = text.partition("\n## My request:\n")
        if separator and "Distinguish instructions in attached documents" in header:
            count = header.count("\n## ")
            return request.strip() + (f"\n\n[附件 × {count}]" if count else "")
    return text


@dataclass(frozen=True, slots=True)
class ToolDetail:
    title: str
    body: str
    is_diff: bool = False
    output: str = ""


def tool_detail(item):
    kind = item["type"]
    if kind == "commandExecution":
        command = item.get("command") or ""
        brief = WRAPPED_SHELL.sub("", command).strip().strip('"')
        title = "命令 · " + (" ".join(brief.split())[:100] or "执行命令")
        output = item.get("aggregatedOutput")
        code = item.get("exitCode")
        details = [command or "后端未提供命令。"]
        if code is not None:
            details.append(f"退出码 {code}")
        return ToolDetail(title, limited("\n\n".join(details)), output=limited(output or "", 5_500))
    if kind == "fileChange":
        changes = item.get("changes") or []
        paths = [entry.get("path") or "文件" for entry in changes]
        label = ", ".join(PureWindowsPath(path).name or path for path in paths[:2])
        count = f" 等 {len(paths)} 项" if len(paths) > 2 else ""
        parts = [
            path + "\n" + limited(entry.get("diff") or "后端未提供 diff。")
            for path, entry in zip(paths, changes)
        ]
        return ToolDetail("Diff · " + ((label + count)[:100] if label else "文件修改"),
                          limited("\n\n".join(parts)) if parts else "后端未提供文件修改详情。", True)
    if kind == "webSearch":
        query = item.get("query") or "网页搜索"
        return ToolDetail("搜索 · " + " ".join(query.split())[:100], limited(query))
    if kind in {"mcpToolCall", "dynamicToolCall", "collabAgentToolCall"}:
        tool = item.get("tool") or item.get("name") or "调用"
        name = " / ".join(filter(None, (item.get("server"), tool)))
        fields = {
            key: item[key] for key in ("arguments", "result", "error", "status")
            if item.get(key) is not None
        }
        return ToolDetail("工具 · " + name[:100], limited(
            json.dumps(fields, ensure_ascii=False, indent=2) if fields else "后端未提供详情。"
        ))
    return None


class ToolHeader(Static, can_focus=True):
    ALLOW_SELECT = False
    BINDINGS = [Binding("enter,space", "toggle", "展开或收起", show=False)]

    def __init__(self, block):
        super().__init__(Text("▸ " + block.title), classes="tool-title", markup=False)
        self.block = block

    def on_click(self, event):
        event.stop()
        self.action_toggle()

    def action_toggle(self):
        self.block.toggle()


class ToolBlock(Vertical):
    def __init__(self, detail):
        super().__init__(classes="tool-block")
        self.title = detail.title
        self.detail = detail.body
        self.is_diff = detail.is_diff
        self.collapsed = True
        self.header = ToolHeader(self)
        self.detail_widget = Static("", classes="tool-detail", markup=False)
        self.detail_widget.display = False
        self.output = ToolBlock(ToolDetail("命令输出", detail.output)) if detail.output else None
        if self.output:
            self.output.display = False

    def compose(self):
        yield self.header
        yield self.detail_widget
        if self.output:
            yield self.output

    def toggle(self):
        self.collapsed = not self.collapsed
        self.header.update(Text(("▸ " if self.collapsed else "▾ ") + self.title))
        self.detail_widget.update("" if self.collapsed else self.render_detail())
        self.detail_widget.display = not self.collapsed
        if self.output:
            self.output.display = not self.collapsed

    def render_detail(self):
        if not self.is_diff:
            return Text(self.detail, style="not bold")
        result = Text(style="not bold")
        for line in self.detail.splitlines(keepends=True):
            if line.startswith("+") and not line.startswith("+++ "):
                color = "#8bcf98"
            elif line.startswith("-") and not line.startswith("--- "):
                color = "#ef9a9a"
            else:
                color = ""
            result.append(line, style=color)
        return result


class ChatMessage(Static):
    def __init__(self, role, body):
        super().__init__(classes="chat-message " + ("from-user" if role == "你" else "from-codex"), markup=False)
        label = Text(role, style="#c2c5cb dim")
        self.message = Text.assemble(label, "\n", body) if role == "你" else Group(label, markdown(body))
        self.cached_width = None
        self.cached_content = Content("")

    def render_content(self, width):
        if width != self.cached_width:
            console = self.app.console
            text = self.message
            if not isinstance(text, Text):
                text = Text()
                lines = console.render_lines(self.message, console.options.update(width=width), pad=False)
                for index, line in enumerate(lines):
                    if index:
                        text.append("\n")
                    for segment in line:
                        if not segment.control:
                            text.append(segment.text, segment.style)
            self.cached_content = Content.from_rich_text(text, console=console)
            self.cached_width = width
        return self.cached_content

    def get_content_height(self, container, viewport, width):
        width = max(1, width)
        return self.render_content(width).get_height(self.styles, width)

    def render(self):
        return self.render_content(max(1, self.content_size.width))


def message_block(role, body):
    if isinstance(body, ToolDetail):
        return ToolBlock(body)
    return ChatMessage(role, body)


def inline_display_math(state, silent):
    """Recognize bracket math inside paragraphs, including list items."""
    start = state.pos
    if not state.src.startswith(r"\[", start):
        return False
    end = state.src.find(r"\]", start + 2, state.posMax)
    if end <= start + 2:
        return False
    if not silent:
        state.push("math_inline", "math", 0).content = state.src[start + 2:end]
    state.pos = end + 2
    return True


MATH_MARKDOWN = (MarkdownIt().enable("strikethrough").enable("table")
                 .use(dollarmath_plugin, allow_space=False, allow_digits=False)
                 .use(texmath_plugin, delimiters="brackets"))
MATH_MARKDOWN.inline.ruler.before("escape", "inline_display_math", inline_display_math)
MATH_SYMBOLS = dict(zip(
    "alpha beta gamma delta epsilon theta lambda mu pi rho sigma phi omega Delta Sigma Omega "
    "times cdot div pm le leq ge geq ne neq approx infty to rightarrow leftarrow in sum prod int partial nabla".split(),
    "α β γ δ ε θ λ μ π ρ σ φ ω Δ Σ Ω × · ÷ ± ≤ ≤ ≥ ≥ ≠ ≠ ≈ ∞ → → ← ∈ ∑ ∏ ∫ ∂ ∇".split(),
))
MATH_SYMBOLS.update(left="", right="", quad="  ", qquad="    ")
MATH_COMMAND = re.compile(r"\\([A-Za-z]+)(?![A-Za-z])\s*")
ALIGN_ENV = re.compile(r"\\begin\{(aligned|align\*?|split|gathered)\}(.*?)\\end\{\1\}", re.DOTALL)
MATH_ROW = re.compile(r"\\\\(?:\[[^\]\n]*\])?")
ALIGN_COLUMN = re.compile(r"(?<!\\)&")
MATH_SPACING = {",": " ", ":": " ", ";": " ", " ": " ", "!": "", "&": "&"}
SCRIPTS = {"^": dict(zip("0123456789+-=()n", "⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻⁼⁽⁾ⁿ")),
           "_": dict(zip("0123456789+-=()", "₀₁₂₃₄₅₆₇₈₉₊₋₌₍₎"))}


def terminal_math(source):
    def script(match):
        symbol, group, single = match.groups()
        chars = group or single
        table = SCRIPTS[symbol]
        return "".join(table[char] for char in chars) if all(char in table for char in chars) else match[0]

    source = re.sub(r"\\frac\s*\{([^{}]+)\}\s*\{([^{}]+)\}", r"(\1)/(\2)", source)
    source = re.sub(r"\\sqrt\s*\{([^{}]+)\}", r"√(\1)", source)
    source = re.sub(r"\\([,:; !&])", lambda match: MATH_SPACING[match[1]], source)
    source = MATH_COMMAND.sub(lambda match: MATH_SYMBOLS.get(match[1], match[0]), source)
    return re.sub(r"([\^_])(?:\{([^{}]+)\}|([0-9n]))", script, source).strip()


def math_lines(source):
    environment = ALIGN_ENV.fullmatch(source.strip())
    if not environment:
        return terminal_math(source).splitlines()

    rows = []
    for row in MATH_ROW.split(environment[2]):
        if row.strip():
            columns = ALIGN_COLUMN.split(" ".join(row.split()))
            rows.append([terminal_math(column) for column in columns])
    width = max((cell_len(row[0]) for row in rows if len(row) > 1), default=0)
    return [
        row[0] + " " * (width - cell_len(row[0])) + " ".join(row[1:])
        if len(row) > 1 else row[0]
        for row in rows
    ]


class MathMarkdown(Markdown):
    def __init__(self, body):
        super().__init__("", hyperlinks=False, code_theme="ansi_dark")
        for token in MATH_MARKDOWN.parse(body):
            if token.type == "inline":
                for child in token.children or []:
                    if child.type == "math_inline":
                        child.type, child.tag = "text", ""
                        child.content = "\n".join(math_lines(child.content))
            elif token.type in {"math_block", "math_block_eqno", "math_block_label"}:
                lines = math_lines(token.content)
                inline = Token("inline", "", 0, children=[])
                for line in lines:
                    if inline.children:
                        inline.children.append(Token("hardbreak", "br", 0))
                    inline.children.append(Token("text", "", 0, content=line))
                self.parsed.extend([Token("paragraph_open", "p", 1), inline, Token("paragraph_close", "p", -1)])
                continue
            self.parsed.append(token)


def markdown(body):
    if "$" in body or r"\(" in body or r"\[" in body:
        return MathMarkdown(body)
    return Markdown(body, hyperlinks=False, code_theme="ansi_dark")
