"""A small Windows button for handing the visible Codex chat to the TUI."""

import asyncio
import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import tkinter as tk
from tkinter import messagebox
from urllib.parse import urlsplit

from backend import Backend, codex_environment
from shared_owner import close_original
from shared_state import RUNTIME, read_endpoint


ROOT = Path(__file__).parent
USER = ctypes.WinDLL("user32", use_last_error=True)
KERNEL = ctypes.WinDLL("kernel32", use_last_error=True)
USER.GetForegroundWindow.restype = wintypes.HWND
USER.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
USER.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
USER.GetAncestor.restype = wintypes.HWND
USER.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
USER.GetDpiForWindow.argtypes = [wintypes.HWND]
USER.SetWindowPos.argtypes = [
    wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.UINT,
]
USER.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
USER.SetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_long]
USER.SetProcessDpiAwarenessContext.argtypes = [wintypes.HANDLE]
KERNEL.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
KERNEL.OpenProcess.restype = wintypes.HANDLE
KERNEL.QueryFullProcessImageNameW.argtypes = [
    wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD),
]
KERNEL.CloseHandle.argtypes = [wintypes.HANDLE]
KERNEL.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
KERNEL.CreateMutexW.restype = wintypes.HANDLE


def is_desktop(pid):
    process = KERNEL.OpenProcess(0x1000, False, pid)
    if not process:
        return False
    try:
        image = ctypes.create_unicode_buffer(1024)
        size = wintypes.DWORD(len(image))
        if not KERNEL.QueryFullProcessImageNameW(process, 0, image, ctypes.byref(size)):
            return False
        path = Path(image.value)
        return (
            path.name.casefold() == "chatgpt.exe" and path.parent.name.casefold() == "app"
            and path.parent.parent.name.casefold().startswith("openai.codex_")
            and path.parent.parent.parent.name.casefold() == "windowsapps"
        )
    finally:
        KERNEL.CloseHandle(process)


async def desktop_context(pid, hwnd, endpoint):
    process = await asyncio.create_subprocess_exec(
        "powershell.exe", "-NoProfile", "-NonInteractive", "-File", str(ROOT / "desktop_context.ps1"),
        "-DesktopPid", str(pid), "-WindowHandle", str(hwnd), "-RelayPort", str(urlsplit(endpoint).port),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW,
    )
    try:
        stdout, _ = await asyncio.wait_for(process.communicate(), 8)
    except TimeoutError:
        process.kill()
        await process.wait()
        raise RuntimeError("读取桌面聊天与连接状态超时。") from None
    if process.returncode:
        raise RuntimeError("无法确认这个桌面窗口已接入共享后端，或无法读取当前聊天。")
    return json.loads(stdout)


async def find_thread(backend, title):
    matches = []
    for archived in (False, True):
        cursor = None
        while True:
            page = await backend.request("thread/list", {
                "limit": 75, "searchTerm": title, "archived": archived, "cursor": cursor,
                "modelProviders": [],
            })
            matches.extend(
                entry for entry in page["data"] if (entry.get("name") or entry.get("preview")) == title
            )
            cursor = page.get("nextCursor")
            if not cursor or len(matches) > 1:
                break
    if len(matches) != 1 or matches[0].get("source") == "cloud":
        raise RuntimeError("当前标题无法唯一对应本机 Codex 聊天。请切到一条已保存、标题唯一的本地聊天。")
    return matches[0]["id"]


async def require_idle(backend, expected):
    cursor = None
    found = False
    while True:
        page = await backend.request("thread/loaded/list", {"limit": 100, "cursor": cursor})
        for thread_id in page["data"]:
            found = found or thread_id == expected
            result = await backend.request("thread/read", {"threadId": thread_id})
            if result["thread"]["status"]["type"] not in {"idle", "notLoaded"}:
                raise RuntimeError("Codex 还有任务正在运行或等待处理，桌面继续保留。")
        cursor = page.get("nextCursor")
        if not cursor:
            break
    if not found:
        raise RuntimeError("共享后端未加载桌面上的这条聊天，已保留桌面。")


async def handoff(pid, hwnd):
    endpoint = read_endpoint()
    if not endpoint:
        raise RuntimeError("共享后端尚未启用。当前桌面继续运行。")
    original = await desktop_context(pid, hwnd, endpoint)
    title = original["title"].strip()
    if title.casefold() in {"chatgpt", "codex"}:
        raise RuntimeError("请先打开一条已保存的本地 Codex 聊天。")
    backend = Backend(lambda _: asyncio.sleep(0), shared_url=endpoint)
    try:
        await backend.start()
        thread = await find_thread(backend, title)
        await require_idle(backend, thread)
        with tempfile.TemporaryDirectory(prefix="handoff-", dir=RUNTIME) as directory:
            ready = Path(directory) / "ready"
            env = codex_environment()
            env["CODEX_LIGHT_SERVER_URL"] = endpoint
            env.pop("CODEX_LIGHT_SERVER_TOKEN", None)
            executable = Path(sys.executable).with_name("python.exe")
            tui = subprocess.Popen([
                str(executable), "-X", "utf8", str(ROOT / "main.py"),
                "--handoff-thread", thread, "--handoff-ready", str(ready),
            ], env=env, cwd=ROOT, creationflags=subprocess.CREATE_NEW_CONSOLE)
            for _ in range(150):
                if ready.is_file():
                    break
                if tui.poll() is not None:
                    raise RuntimeError("TUI 已退出，桌面继续保留。")
                await asyncio.sleep(.2)
            else:
                raise RuntimeError("TUI 未及时确认接力，桌面继续保留。")
            if await desktop_context(pid, hwnd, endpoint) != original or read_endpoint() != endpoint:
                raise RuntimeError("桌面聊天或连接已变化，桌面继续保留。")
            await require_idle(backend, thread)
            if tui.poll() is not None:
                raise RuntimeError("TUI 已退出，桌面继续保留。")
            await close_original(pid, original["created"])
    finally:
        await backend.close()


def run_overlay():
    USER.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
    root = tk.Tk()
    root.overrideredirect(True)
    root.geometry("1x1-20000-20000")
    root.title("切到 TUI")
    button = tk.Button(
        root, text="切到 TUI", bg="#202020", fg="#dedede", activebackground="#363636",
        activeforeground="white", bd=0, highlightthickness=0, cursor="hand2",
    )
    button.pack(fill="both", expand=True)
    root.update()
    window = USER.GetAncestor(root.winfo_id(), 2)
    USER.SetWindowLongW(window, -20, USER.GetWindowLongW(window, -20) | 0x08000080)
    USER.SetWindowPos(window, None, 0, 0, 0, 0, 0x0093)
    desktop = None
    last = None
    position = None
    worker = None
    result = None

    def switch():
        nonlocal worker
        if not desktop or worker:
            return
        hwnd, pid = desktop
        button.configure(text="正在接力…", state=tk.DISABLED)

        def transfer():
            nonlocal result
            try:
                asyncio.run(handoff(pid, hwnd))
            except (OSError, RuntimeError, ValueError, KeyError, TimeoutError) as error:
                result = (str(error) or "接力超时，桌面继续保留。",)
            else:
                result = ()

        worker = threading.Thread(target=transfer, daemon=True)
        worker.start()

    def button_visibility():
        nonlocal desktop, last, position, worker, result
        if result is not None:
            if not result:
                root.destroy()
                return
            message = result[0]
            worker = result = None
            button.configure(text="切到 TUI", state=tk.NORMAL)
            messagebox.showwarning("切到 TUI", message, parent=root)
        hwnd = USER.GetForegroundWindow()
        if hwnd != window:
            pid = wintypes.DWORD()
            if hwnd:
                USER.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            identity = (hwnd, pid.value)
            if identity != last:
                desktop = identity if hwnd and is_desktop(pid.value) else None
                last = identity
        rect = wintypes.RECT()
        target = None
        if desktop and USER.GetWindowRect(desktop[0], ctypes.byref(rect)):
            dpi = USER.GetDpiForWindow(desktop[0]) or 96
            scale = dpi / 96
            offset, width, height = (round(value * scale) for value in (161, 80, 27))
            if rect.right - rect.left >= round(620 * scale):
                target = rect.right - offset - width, rect.top + round(7 * scale), width, height
        if target != position:
            if target:
                USER.SetWindowPos(window, -1, *target, 0x0010 | 0x0040)
            else:
                USER.SetWindowPos(window, None, 0, 0, 0, 0, 0x0093)
            position = target
        root.after(250, button_visibility)

    button.configure(command=switch)
    menu = tk.Menu(root, tearoff=False)
    menu.add_command(label="退出浮动按钮", command=root.destroy)
    button.bind("<Button-3>", lambda event: menu.tk_popup(event.x_root, event.y_root))
    button_visibility()
    root.mainloop()


if __name__ == "__main__":
    mutex = KERNEL.CreateMutexW(None, False, "Local\\CodexLightDesktopButton")
    if mutex:
        try:
            if ctypes.get_last_error() != 183:
                run_overlay()
        finally:
            KERNEL.CloseHandle(mutex)
