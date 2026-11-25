"""Terminal composer and explicit access to the Windows clipboard."""

import asyncio
import base64
import os
from pathlib import Path
import subprocess
from uuid import uuid4

from textual.widgets import TextArea


class Composer(TextArea):
    def __init__(self, *, submit=None, menu_key=None, **kwargs):
        super().__init__(**kwargs)
        self.submit_message = submit
        self.menu_key = menu_key

    async def _on_key(self, event):
        if self.read_only:
            return
        key = event.key
        if key in {"up", "down", "escape"}:
            handler = self.menu_key if self.submit_message is not None else self.app.slash_menu_key
            if handler is not None and handler(key):
                event.stop()
                event.prevent_default()
                return
        if self.submit_message is not None:
            if key in {"enter", "ctrl+enter", "shift+enter", "ctrl+j"}:
                event.stop()
                event.prevent_default()
                if key in {"enter", "ctrl+enter"}:
                    self.submit_message()
                else:
                    self.insert_paste("\n")
                return
            await super()._on_key(event)
            return
        if key in {"enter", "shift+enter", "ctrl+j", "ctrl+alt+v", "ctrl+v"}:
            event.stop()
            event.prevent_default()
            if key == "enter":
                self.app.action_send_prompt()
            elif key in {"shift+enter", "ctrl+j"}:
                self.insert_paste("\n")
            else:
                self.app.paste_clipboard(allow_text=key == "ctrl+v")
        else:
            await super()._on_key(event)

    def insert_paste(self, text):
        result = self.replace(text, *self.selection, maintain_selection_offset=False)
        self.move_cursor(result.end_location)


CLIPBOARD_READ = r"""
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
Add-Type -AssemblyName System.Windows.Forms
$clipboard = [System.Windows.Forms.Clipboard]
for ($attempt = 0; $attempt -lt 3; $attempt++) {
    try { $data = $clipboard::GetDataObject(); break }
    catch [System.Runtime.InteropServices.ExternalException] {
        if ($attempt -eq 2) { throw }
        Start-Sleep -Milliseconds 50
    }
}
if ($null -eq $data) { exit 0 }
if ($data.GetDataPresent('PNG')) {
    $png = $data.GetData('PNG')
    if ($png -is [System.IO.MemoryStream]) {
        [System.IO.File]::WriteAllBytes($env:CODEX_LIGHT_PASTE_DEST, $png.ToArray())
        exit 0
    }
    if ($png -is [byte[]]) {
        [System.IO.File]::WriteAllBytes($env:CODEX_LIGHT_PASTE_DEST, $png)
        exit 0
    }
}
if ($clipboard::ContainsImage()) {
    $image = $clipboard::GetImage()
    if ($null -ne $image) {
        try { $image.Save($env:CODEX_LIGHT_PASTE_DEST, [System.Drawing.Imaging.ImageFormat]::Png) }
        finally { $image.Dispose() }
        exit 0
    }
}
if ($env:CODEX_LIGHT_PASTE_TEXT -eq '1' -and $clipboard::ContainsText()) {
    [Console]::Write($clipboard::GetText())
}
"""


async def read_clipboard(allow_text):
    directory = Path(__file__).with_name(".pasted-images")
    directory.mkdir(exist_ok=True)
    path = directory / f"paste-{uuid4().hex[:12]}.png"
    env = os.environ | {
        "CODEX_LIGHT_PASTE_DEST": str(path),
        "CODEX_LIGHT_PASTE_TEXT": str(int(allow_text)),
    }
    process = await asyncio.create_subprocess_exec(
        "powershell.exe", "-NoProfile", "-NonInteractive", "-STA", "-Command", CLIPBOARD_READ,
        env=env, stdout=asyncio.subprocess.PIPE, stderr=subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    output, _ = await process.communicate()
    if process.returncode:
        path.unlink(missing_ok=True)
        raise OSError("暂时无法读取 Windows 剪贴板。")
    if path.is_file():
        with path.open("rb") as stream:
            valid = stream.read(8) == b"\x89PNG\r\n\x1a\n"
        if not valid:
            path.unlink()
            raise OSError("剪贴板图片未能转换成 PNG。")
        return path, ""
    return None, output.decode("utf-8", errors="replace")


async def copy_clipboard(text):
    command = ("$ErrorActionPreference = 'Stop'; "
               "$bytes = [Convert]::FromBase64String([Console]::In.ReadToEnd()); "
               "Set-Clipboard -Value ([Text.Encoding]::UTF8.GetString($bytes))")
    process = await asyncio.create_subprocess_exec(
        "powershell.exe", "-NoProfile", "-NonInteractive", "-STA", "-Command", command,
        stdin=asyncio.subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    await process.communicate(base64.b64encode(text.encode("utf-8")))
    if process.returncode:
        raise OSError("暂时无法写入 Windows 剪贴板。")
