"""JSONL transport for the official Codex App Server."""

import asyncio
import json
import os
from pathlib import Path
import shutil
import subprocess
from urllib.parse import urlsplit
import winreg


class CodexError(RuntimeError):
    pass


class ConnectionInterrupted(CodexError):
    pass


def codex_environment():
    env = os.environ.copy()
    if not env.get("CODEX_HOME"):
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
                env["CODEX_HOME"] = os.path.expandvars(winreg.QueryValueEx(key, "CODEX_HOME")[0])
        except OSError:
            pass
    return env


def codex_executable(env):
    if env.get("CODEX_LIGHT_EXE"):
        return env["CODEX_LIGHT_EXE"]
    roots = [Path(env["CODEX_HOME"]).parent] if env.get("CODEX_HOME") else []
    npm_launcher = shutil.which("codex.cmd", path=env.get("PATH"))
    if npm_launcher:
        roots.append(Path(npm_launcher).parent)
    for root in roots:
        package = root / "node_modules/@openai/codex"
        for base in (package, package.parent):
            binaries = base.glob(
                "node_modules/@openai/codex-win32-*/vendor/*/bin/codex.exe"
                if base == package else "codex-win32-*/vendor/*/bin/codex.exe"
            )
            for binary in binaries:
                return str(binary)
    executable = shutil.which("codex.exe", path=env.get("PATH"))
    if executable:
        return executable
    raise CodexError("找不到 Codex。请将 CODEX_LIGHT_EXE 指向官方 codex.exe。")


CLIENT_VERSION = "0.2.0"


class Backend:
    def __init__(self, on_event, *, shared_url=None):
        self.on_event = on_event
        self.process = None
        self.socket = None
        self.fixed_shared_url = shared_url or os.environ.get("CODEX_LIGHT_SERVER_URL", "").strip()
        self.shared_url = self.fixed_shared_url
        self.shared_mode = bool(self.shared_url)
        self.reader = None
        self.pending = {}
        self.serial = 0

    async def start(self):
        if not self.fixed_shared_url:
            from shared_state import read_endpoint
            try:
                endpoint = read_endpoint()
            except (OSError, ValueError, KeyError) as error:
                self.shared_mode = True
                raise CodexError("无法读取已配置的共享后端，已停止启动，避免启用第二个后端。") from error
            self.shared_mode |= bool(endpoint)
            self.shared_url = endpoint
            if self.shared_mode and not endpoint:
                raise CodexError("共享后端正在恢复，等待新的本机连接地址。")
        if self.shared_url:
            await self.connect_shared()
        else:
            env = codex_environment()
            self.process = await asyncio.create_subprocess_exec(
                codex_executable(env), "app-server", "--listen", "stdio://",
                "-c", "thread_unload_delay_secs=0",
                env=env, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW,
                limit=32 * 1024 * 1024,
            )
        self.reader = asyncio.create_task(self.read_messages())
        await self.request("initialize", {
            "clientInfo": {"name": "codex_light_tui", "title": "Codex Light TUI", "version": CLIENT_VERSION},
            "capabilities": {"experimentalApi": True},
        })
        await self.send({"method": "initialized"})

    async def connect_shared(self):
        try:
            from websockets.asyncio.client import connect
            from websockets.exceptions import WebSocketException
        except ImportError as error:
            raise CodexError("共享模式需要安装项目 requirements.txt 中的 websockets。") from error
        try:
            parsed = urlsplit(self.shared_url)
            if (parsed.scheme not in {"ws", "wss"} or parsed.hostname not in {"127.0.0.1", "::1"}
                    or parsed.username or parsed.password or parsed.fragment or parsed.query):
                raise ValueError
            token = os.environ.get("CODEX_LIGHT_SERVER_TOKEN")
            headers = {"Authorization": f"Bearer {token}"} if token else None
            self.socket = await connect(
                self.shared_url, additional_headers=headers, proxy=None, open_timeout=4,
                close_timeout=2, max_size=32 * 1024 * 1024,
            )
        except (OSError, TimeoutError, ValueError, WebSocketException) as error:
            raise CodexError("连接共享后端失败。请核对本机回环地址、端口和访问凭据。") from error

    async def send(self, message):
        wire = json.dumps(message, ensure_ascii=False, separators=(",", ":"))
        if self.socket is not None:
            from websockets.exceptions import WebSocketException
            try:
                await self.socket.send(wire)
            except WebSocketException as error:
                raise ConnectionInterrupted("共享 Codex 后端连接已关闭。") from error
            return
        if self.process is None or self.process.returncode is not None:
            raise ConnectionInterrupted("Codex 后端已断开。")
        self.process.stdin.write(wire.encode("utf-8") + b"\n")
        await self.process.stdin.drain()

    async def request(self, method, params=None):
        self.serial += 1
        request_id = self.serial
        future = asyncio.get_running_loop().create_future()
        self.pending[request_id] = future
        try:
            data = {"id": request_id, "method": method}
            if params is not None:
                data["params"] = params
            await self.send(data)
            return await asyncio.wait_for(future, 30)
        except TimeoutError as error:
            raise ConnectionInterrupted(f"{method} 等待超时。") from error
        finally:
            self.pending.pop(request_id, None)

    async def incoming(self):
        if self.socket is not None:
            from websockets.exceptions import WebSocketException
            try:
                async for frame in self.socket:
                    yield frame
            except WebSocketException as error:
                raise OSError("共享 WebSocket 已断开。") from error
        else:
            while line := await self.process.stdout.readline():
                yield line

    async def read_messages(self):
        detail = "Codex 后端已关闭。"
        try:
            async for line in self.incoming():
                message = json.loads(line)
                if "method" in message:
                    # The UI queues requests immediately and runs dialogs in separate workers.
                    await self.on_event(message)
                    continue
                future = self.pending.get(message.get("id"))
                if future and not future.done():
                    if "error" in message:
                        future.set_exception(CodexError(message["error"]["message"]))
                    else:
                        future.set_result(message.get("result"))
        except (OSError, ValueError) as error:
            detail = f"Codex 通讯中断：{error}"
        finally:
            for future in self.pending.values():
                if not future.done():
                    future.set_exception(ConnectionInterrupted(detail))
            await self.on_event({"method": "connection/closed", "params": {"message": detail}})

    async def close(self):
        if self.socket is not None:
            await self.socket.close()
            self.socket = None
        if self.process and self.process.returncode is None:
            self.process.stdin.close()
            try:
                await asyncio.wait_for(self.process.wait(), 4)
            except TimeoutError:
                self.process.terminate()
                await self.process.wait()
        if self.reader:
            await self.reader
