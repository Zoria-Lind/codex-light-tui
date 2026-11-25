from pathlib import Path

from rich.text import Text
from textual import on
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, OptionList, Select, Static


class CancellableDialog(ModalScreen):
    BINDINGS = [("escape", "dismiss", "返回")]

    @on(Button.Pressed, "#cancel-dialog")
    def cancel_dialog(self, event):
        event.stop()
        self.dismiss()


class Settings(CancellableDialog):
    def __init__(self, permission):
        super().__init__()
        self.permission = permission

    def compose(self):
        with Vertical(classes="dialog"):
            yield Label("设置 · 会话权限")
            yield Select(
                [("沿用 Codex 设置", ""), ("只读 · 按需审批", "readOnly"),
                 ("工作区写入 · 按需审批", "workspaceWrite")],
                value=self.permission, allow_blank=False, id="permission",
            )
            with Horizontal(classes="dialog-buttons"):
                yield Button("取消", id="cancel-dialog")
                yield Button("应用", variant="primary", id="apply")

    @on(Button.Pressed, "#apply")
    def apply(self):
        self.dismiss(self.query_one("#permission", Select).value)


class Picker(CancellableDialog):
    def __init__(self, title, choices, selected):
        super().__init__()
        self.caption = title
        self.choices = choices
        self.selected = selected

    def compose(self):
        with Vertical(classes="dialog"):
            yield Label(self.caption)
            yield OptionList(*(Text(label) for label, _ in self.choices), id="picker-options")
            yield Button("取消", id="cancel-dialog")

    def on_mount(self):
        options = self.query_one(OptionList)
        options.highlighted = next((i for i, (_, value) in enumerate(self.choices) if value == self.selected), 0)
        options.focus()

    @on(OptionList.OptionSelected)
    def choose(self, event):
        self.dismiss(self.choices[event.option_index][1])


class StatusDialog(CancellableDialog):
    def __init__(self, detail):
        super().__init__()
        self.detail = detail

    def compose(self):
        with Vertical(classes="dialog"):
            yield Label("状态 · 当前主聊天")
            with VerticalScroll(classes="dialog-body"):
                yield Static(Text(self.detail), id="status-details")
            with Horizontal(classes="dialog-buttons"):
                yield Button("关闭", id="cancel-dialog", variant="primary")

    def update_detail(self, detail):
        self.query_one("#status-details", Static).update(Text(detail))


class PathDialog(CancellableDialog):
    def __init__(self, root, images, mode):
        super().__init__()
        self.root = Path(root)
        self.images = images
        self.mode = mode

    def compose(self):
        with Vertical(classes="dialog"):
            yield Label({"attach": "添加文件或图片", "image": "打开本地图片", "project": "新聊天的项目目录"}[self.mode])
            yield Input(placeholder="粘贴路径", id="path", value=str(self.root) if self.mode == "project" else "")
            if self.mode == "image" and self.images:
                yield Select([(Path(path).name, path) for path in self.images], prompt="从聊天中选择图片", id="images")
            yield Label("", id="path-error")
            with Horizontal(classes="dialog-buttons"):
                yield Button("取消", id="cancel-dialog")
                yield Button("确定", variant="primary", id="confirm")

    @on(Select.Changed, "#images")
    def choose_image(self, event):
        if isinstance(event.value, str):
            self.query_one("#path", Input).value = event.value

    @on(Button.Pressed, "#confirm")
    @on(Input.Submitted, "#path")
    def confirm(self):
        raw = self.query_one("#path", Input).value.strip().strip('"')
        try:
            if not raw:
                raise ValueError
            path = (self.root / Path(raw).expanduser()).resolve(strict=True)
            if self.mode == "project" and not path.is_dir():
                raise ValueError
            if self.mode != "project" and not path.is_file():
                raise ValueError
            if self.mode == "image" and path.suffix.lower() not in {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}:
                raise ValueError
        except (OSError, ValueError, RuntimeError):
            self.query_one("#path-error", Label).update("请输入有效的本地路径。")
            return
        self.dismiss(str(path))


class Approval(ModalScreen):
    BINDINGS = [("escape", "leave", "返回")]

    def __init__(self, title, detail, can_accept, *, defer=False):
        super().__init__()
        self.title_text = title
        self.detail = detail
        self.can_accept = can_accept
        self.defer = defer

    def compose(self):
        with Vertical(classes="dialog"):
            yield Label(self.title_text)
            with VerticalScroll(classes="dialog-body"):
                yield Static(Text(self.detail))
            with Horizontal(classes="dialog-buttons"):
                if self.defer:
                    yield Button("稍后", id="defer")
                yield Button("拒绝", id="decline")
                yield Button("允许本次", variant="warning", id="accept", disabled=not self.can_accept)

    @on(Button.Pressed)
    def decide(self, event):
        self.dismiss(event.button.id)

    def action_leave(self):
        self.dismiss("defer" if self.defer else "decline")


class ProjectDialog(CancellableDialog):
    def __init__(self, directory):
        super().__init__()
        self.directory = str(directory)

    def compose(self):
        with Vertical(classes="dialog"):
            yield Label("新建本地 Codex 项目")
            yield Input(placeholder="项目名称", id="project-name")
            yield Input(placeholder="现有目录路径", id="project-path", value=self.directory)
            yield Label("", id="path-error")
            with Horizontal(classes="dialog-buttons"):
                yield Button("取消", id="cancel-dialog")
                yield Button("创建", id="create-project", variant="primary")

    @on(Input.Submitted)
    @on(Button.Pressed, "#create-project")
    def submit(self):
        name = self.query_one("#project-name", Input).value.strip()
        directory = self.query_one("#project-path", Input).value.strip().strip('"')
        try:
            if not name or not directory:
                raise ValueError
            path = Path(directory).expanduser().resolve(strict=True)
            if not path.is_dir():
                raise ValueError
        except (OSError, ValueError, RuntimeError):
            self.query_one("#path-error", Label).update("请输入项目名称和有效的本地目录。")
            return
        self.dismiss((name, str(path)))


class Questions(CancellableDialog):
    def __init__(self, questions):
        super().__init__()
        self.questions = questions
        self.step = 0
        self.selected = {}

    def compose(self):
        with Vertical(classes="dialog"):
            yield Label("", id="question-progress")
            with VerticalScroll(classes="dialog-body"):
                for index, question in enumerate(self.questions):
                    with Vertical(id=f"question-{index}", classes="question-page"):
                        yield Static(Text(question["question"]))
                        options = question.get("options") or []
                        for number, option in enumerate(options):
                            detail = option.get("description") or ""
                            yield Button(Text(option["label"] + (f" ? {detail}" if detail else "")),
                                         id=f"choice-{index}-{number}", classes="answer-choice")
                        if options and question.get("isOther"):
                            yield Button("其他", id=f"choice-{index}-other", classes="answer-choice")
                        if not options or question.get("isOther"):
                            yield Input(placeholder="输入你的回答", id=f"answer-{index}",
                                        password=bool(question.get("isSecret")))
            yield Label("", id="question-error")
            with Horizontal(classes="dialog-buttons"):
                yield Button("稍后", id="cancel-dialog")
                yield Button("上一步", id="question-back")
                yield Button("下一步", id="question-next", variant="primary")

    def on_mount(self):
        self.render_step()

    def render_step(self):
        self.query_one("#question-progress", Label).update(f"Codex 提问 · {self.step + 1}/{len(self.questions)}")
        self.query_one("#question-back", Button).disabled = self.step == 0
        next_button = self.query_one("#question-next", Button)
        next_button.label = "提交" if self.step + 1 == len(self.questions) else "下一步"
        self.query_one("#question-error", Label).update("")
        for index, question in enumerate(self.questions):
            self.query_one(f"#question-{index}").display = index == self.step
            answer = next(self.query(f"#answer-{index}").results(Input), None)
            if answer:
                answer.display = not question.get("options") or self.selected.get(index) == "other"
        choices = self.query_one(f"#question-{self.step}").query(Button)
        answer = next(self.query(f"#answer-{self.step}").results(Input), None)
        if answer is not None and answer.display:
            answer.focus()
        elif self.selected.get(self.step) is not None:
            next_button.focus()
        elif choices:
            choices.first().focus()

    @on(Button.Pressed, ".answer-choice")
    def choose(self, event):
        index, choice = event.button.id.split("-")[1:]
        self.selected[int(index)] = choice
        for button in self.query_one(f"#question-{index}").query(Button):
            button.variant = "success" if button is event.button else "default"
        self.render_step()

    @on(Button.Pressed, "#question-back")
    def back(self):
        self.step -= 1
        self.render_step()

    def answer_for(self, index):
        options = self.questions[index].get("options") or []
        chosen = self.selected.get(index)
        if not options or chosen == "other":
            return self.query_one(f"#answer-{index}", Input).value.strip()
        if chosen is not None:
            return options[int(chosen)]["label"]
        return ""

    @on(Button.Pressed, "#question-next")
    @on(Input.Submitted)
    def answer(self):
        if not self.answer_for(self.step):
            self.query_one("#question-error", Label).update("请选择一个选项或填写答案。")
            return
        if self.step + 1 < len(self.questions):
            self.step += 1
            self.render_step()
            return
        self.dismiss({question["id"]: {"answers": [self.answer_for(index)]}
                      for index, question in enumerate(self.questions)})
