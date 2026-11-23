"""One-time native desktop cutover and lifetime owner for a loopback App Server."""

import argparse
import asyncio
import ctypes
from ctypes import wintypes
import hashlib
import hmac
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import time
from urllib.parse import urlsplit

from websockets.asyncio.client import connect
from websockets.asyncio.server import serve
from websockets.exceptions import WebSocketException

from backend import Backend, codex_environment, codex_executable
from shared_state import RUNTIME, STATUS, read_endpoint, write_endpoint


ROOT = Path(__file__).parent
CREATE_HIDDEN = subprocess.CREATE_NO_WINDOW


def status(phase, **fields):
    pending = STATUS.with_suffix(f".{os.getpid()}.tmp")
    pending.write_text(json.dumps({"phase": phase, **fields}, ensure_ascii=False, indent=2), encoding="utf-8")
    pending.replace(STATUS)


class JobLimits(ctypes.Structure):
    _fields_ = [("time_limits", ctypes.c_int64 * 2), ("flags", wintypes.DWORD),
                ("working_set", ctypes.c_size_t * 2), ("process_limit", wintypes.DWORD),
                ("affinity", ctypes.c_size_t), ("priority", wintypes.DWORD),
                ("scheduling", wintypes.DWORD), ("io_counters", ctypes.c_uint64 * 6),
                ("memory_limits", ctypes.c_size_t * 4)]


def job_for_process(pid):
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    kernel.CreateJobObjectW.restype = wintypes.HANDLE
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    job = kernel.CreateJobObjectW(None, None)
    if not job:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        limits = JobLimits()
        limits.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not kernel.SetInformationJobObject(job, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
            raise ctypes.WinError(ctypes.get_last_error())
        process = kernel.OpenProcess(0x101, False, pid)  # PROCESS_SET_QUOTA | PROCESS_TERMINATE
        if not process:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            if not kernel.AssignProcessToJobObject(job, process):
                raise ctypes.WinError(ctypes.get_last_error())
        finally:
            kernel.CloseHandle(process)
    except OSError:
        kernel.CloseHandle(job)
        raise
    return lambda: kernel.CloseHandle(job)


def outstanding(path):
    active = set()
    with path.open("rb") as stream:
        for line in stream:
            try:
                record = json.loads(line)
            except ValueError:
                if not line.endswith(b"\n"):
                    break
                raise
            if record.get("type") != "event_msg":
                continue
            payload = record.get("payload") or {}
            turn_id = payload.get("turn_id")
            if not turn_id:
                continue
            if payload.get("type") == "task_started":
                active.add(turn_id)
            elif payload.get("type") in {"task_complete", "turn_aborted"}:
                active.discard(turn_id)
    return active


async def wait_idle(sessions, earliest, current, turn, report=status):
    started = time.monotonic()
    cache = {}
    quiet_since = None
    last_bad = None
    while time.monotonic() - started < 30 * 60:
        try:
            files = {path for path in sessions.rglob("*.jsonl") if path.stat().st_mtime >= earliest}
            if current is not None:
                files.add(current)
            for path in files:
                metadata = path.stat()
                stamp = metadata.st_mtime_ns, metadata.st_size
                if path not in cache or cache[path][0] != stamp:
                    cache[path] = stamp, await asyncio.to_thread(outstanding, path)
            blockers = sum(len(cache[path][1]) for path in files)
            current_active = current is not None and turn in cache[current][1]
        except (OSError, ValueError):
            blockers, current_active = -1, True
        waiting = (blockers, current_active)
        if waiting != last_bad:
            report("等待现有任务结束", active_turns=blockers, current_turn_active=current_active)
            last_bad = waiting
        now = time.monotonic()
        quiet_since = (quiet_since or now) if blockers == 0 and not current_active else None
        if quiet_since and now - quiet_since >= 5:
            return
        await asyncio.sleep(2)
    raise RuntimeError("等待超时，正式桌面仍保持原状态。")


async def powershell(code):
    command = await asyncio.create_subprocess_exec(
        "powershell.exe", "-NoProfile", "-NonInteractive", "-Command", code,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, creationflags=CREATE_HIDDEN,
    )
    stdout, _ = await command.communicate()
    if command.returncode:
        raise RuntimeError("Windows 进程核对失败。")
    return stdout.decode("utf-8-sig").strip()


async def close_original(pid, expected_start):
    command = await asyncio.create_subprocess_exec(
        "powershell.exe", "-NoProfile", "-NonInteractive", "-File", str(ROOT / "desktop_quit.ps1"),
        "-DesktopPid", str(pid), "-ExpectedStart", str(expected_start),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, creationflags=CREATE_HIDDEN,
    )
    try:
        stdout, _ = await asyncio.wait_for(command.communicate(), 38)
    except TimeoutError:
        command.kill()
        await command.wait()
        raise RuntimeError("读取正式退出菜单超时，未启动新的官方后端。") from None
    result = stdout.decode("ascii", errors="replace").strip()
    if command.returncode:
        raise RuntimeError("无法调用正式桌面的退出菜单。")
    if result not in {"closed", "already-closed"}:
        raise RuntimeError(f"正式桌面退出未确认（{result}）。")


async def verify_original_exited(backend_pid):
    for _ in range(32):
        if not await powershell(f"if(Get-Process -Id {backend_pid} -ErrorAction SilentlyContinue){{'running'}}"):
            break
        await asyncio.sleep(.45)
    else:
        raise RuntimeError("原桌面后端尚未退出，暂不启动第二个后端。")
    roots = await powershell(
        "Get-CimInstance Win32_Process -Filter \"Name='ChatGPT.exe'\" | "
        "Where-Object { $_.ExecutablePath -match 'WindowsApps\\\\OpenAI.Codex_' -and "
        "$_.CommandLine -notmatch '--type=' } | Select-Object -ExpandProperty ProcessId"
    )
    if roots:
        raise RuntimeError("检测到重新启动的官方桌面，已停止自动切换。")


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class SharedOwner:
    def __init__(self, args):
        self.args = args
        self.env = codex_environment()
        self.home = Path(self.env["CODEX_HOME"])
        self.alias = Path(self.env["LOCALAPPDATA"]) / "Microsoft/WindowsApps/chatgpt-desktop.exe"
        self.secret = secrets.token_urlsafe(32)
        self.token = secrets.token_urlsafe(32)
        self.url = f"ws://127.0.0.1:{free_port()}"
        self.server = None
        self.desktop = None
        self.desktop_initialized = asyncio.Event()
        self.desktop_alive = False
        self.desktop_thread_seen = asyncio.Event()
        self.cli_thread_seen = asyncio.Event()

    def desktop_environment(self, endpoint=""):
        env = self.env.copy()
        for key in ("CODEX_APP_SERVER_WS_URL", "CODEX_ELECTRON_USER_DATA_PATH", "CODEX_APP_SERVER_FORCE_CLI"):
            env.pop(key, None)
        if endpoint:
            env["CODEX_APP_SERVER_WS_URL"] = endpoint
        return env

    async def start_server(self):
        sha = hashlib.sha256(self.token.encode()).hexdigest()
        self.server = await asyncio.create_subprocess_exec(
            codex_executable(self.env), "app-server", "--listen", self.url,
            "--ws-auth", "capability-token", "--ws-token-sha256", sha,
            env=self.env, cwd=ROOT, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, creationflags=CREATE_HIDDEN,
        )
        for _ in range(55):
            try:
                async with self.upstream():
                    return
            except (OSError, WebSocketException):
                if self.server.returncode is not None:
                    break
                await asyncio.sleep(.12)
        raise RuntimeError("正式共享后端未能启动。")

    def upstream(self):
        return connect(
            self.url, additional_headers={"Authorization": f"Bearer {self.token}"},
            proxy=None, open_timeout=4, max_size=32 * 1024 * 1024,
        )

    def authorize(self, connection, request):
        # Browser origins are unnecessary for both native clients.
        if request.headers.get("Origin"):
            return connection.respond(403, "Forbidden")
        if request.path == "/":
            headers = request.headers.get_all("Authorization")
            if len(headers) == 1 and hmac.compare_digest(headers[0].encode(), f"Bearer {self.secret}".encode()):
                return None
        elif hmac.compare_digest(request.path.encode(), ("/" + self.secret).encode()):
            return None
        return connection.respond(404, "Not found")

    async def relay(self, client):
        requests = {}
        is_desktop = False
        is_cli = client.request.path == "/"

        async def downstream(upstream):
            nonlocal is_desktop
            async for raw in client:
                msg = json.loads(raw)
                method = msg.get("method")
                if method == "initialize":
                    name = (msg.get("params", {}).get("clientInfo") or {}).get("name", "")
                    is_desktop = not is_cli and name != "codex_light_tui"
                if (is_desktop or is_cli) and "id" in msg and method:
                    thread = (msg.get("params") or {}).get("threadId")
                    requests[msg["id"]] = method, thread
                await upstream.send(raw)

        async def upstream_reader(upstream):
            async for raw in upstream:
                msg = json.loads(raw)
                if (is_desktop or is_cli) and "result" in msg and msg.get("id") in requests:
                    method, thread = requests.pop(msg["id"])
                    if is_desktop and method == "initialize":
                        self.desktop_alive = True
                        self.desktop_initialized.set()
                    if method in {"thread/read", "thread/resume"} and thread == self.args.thread:
                        (self.cli_thread_seen if is_cli else self.desktop_thread_seen).set()
                elif "error" in msg:
                    requests.pop(msg.get("id"), None)
                await client.send(raw)

        try:
            async with self.upstream() as upstream:
                pipes = [asyncio.create_task(f(upstream)) for f in (downstream, upstream_reader)]
                _, pending = await asyncio.wait(pipes, return_when=asyncio.FIRST_COMPLETED)
                for task in pending:
                    task.cancel()
                await asyncio.gather(*pipes, return_exceptions=True)
        except (WebSocketException, OSError, ValueError):
            return

    async def start_desktop(self, endpoint=""):
        command = [str(self.alias)]
        if self.args.thread:
            command.append(f"codex://threads/{self.args.thread}")
        return await asyncio.create_subprocess_exec(
            *command,
            env=self.desktop_environment(endpoint), cwd=ROOT,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )

    async def verify_tui_connection(self, endpoint):
        tester = Backend(lambda _: asyncio.sleep(0), shared_url=endpoint)
        try:
            await tester.start()
            result = await tester.request("thread/read", {"threadId": self.args.thread})
            if result["thread"]["id"] != self.args.thread or tester.process is not None:
                raise RuntimeError("TUI 未从同一后端读到正式会话。")
            account = await tester.request("account/read", {"refreshToken": False})
            return bool(account.get("account"))
        finally:
            await tester.close()

    async def verify_live_turn(self, endpoint):
        active = None
        finished = asyncio.Event()
        checks = {"delta": False, "completed": False, "approval": False}

        async def event(msg):
            p = msg.get("params") or {}
            if not active or p.get("threadId") != active:
                return
            if msg["method"] == "item/agentMessage/delta":
                checks["delta"] = True
            elif msg["method"] == "turn/completed":
                checks["completed"] = (p.get("turn") or {}).get("status") == "completed"
                finished.set()
            elif "id" in msg:
                checks["approval"] = True

        tester = Backend(event, shared_url=endpoint)
        try:
            await tester.start()
            test = await tester.request("thread/start", {"cwd": str(ROOT), "ephemeral": True, "sandbox": "read-only"})
            active = test["thread"]["id"]
            await tester.request("turn/start", {
                "threadId": active,
                "input": [{"type": "text", "text": "仅回复 WU-TUI-003-OK。不要调用工具。"}],
                "sandboxPolicy": {"type": "readOnly", "networkAccess": False},
            })
            await asyncio.wait_for(finished.wait(), 40)
        except (OSError, RuntimeError, TimeoutError) as error:
            checks["model_check_error"] = str(error) or type(error).__name__
        finally:
            await tester.close()
        return checks

    async def verify_clients(self, endpoint, signed_in):
        evidence = dict(backend_pid=self.server.pid, tui_verified=True, signed_in=signed_in)
        status("共享后端运行中，正在核对正式桌面握手", **evidence)
        try:
            await asyncio.wait_for(self.desktop_initialized.wait(), 40)
            try:
                await asyncio.wait_for(self.desktop_thread_seen.wait(), 12)
            except TimeoutError:
                pass
            evidence.update(desktop_verified=True, same_thread_on_desktop=self.desktop_thread_seen.is_set())
            if self.args.verify_cli:
                await asyncio.create_subprocess_exec(
                    sys.executable, "-X", "utf8", str(ROOT / "official_cli.py"), "resume", self.args.thread,
                    env=self.env, cwd=ROOT, creationflags=subprocess.CREATE_NEW_CONSOLE,
                )
                try:
                    await asyncio.wait_for(self.cli_thread_seen.wait(), 30)
                except TimeoutError:
                    pass
                evidence["same_thread_on_official_cli"] = self.cli_thread_seen.is_set()
            if signed_in:
                evidence["live_test"] = await self.verify_live_turn(endpoint)
            status("正式共享后端已连接", **evidence)
        except (OSError, ValueError, RuntimeError, TimeoutError, WebSocketException) as error:
            status("共享后端仍在运行，客户端验收未完成", **evidence,
                   failure=type(error).__name__)

    async def keep_serving(self, endpoint, signed_in):
        verification = asyncio.create_task(self.verify_clients(endpoint, signed_in))
        try:
            await self.server.wait()
        finally:
            verification.cancel()
            await asyncio.gather(verification, return_exceptions=True)
        status("共享后端已退出", backend_pid=self.server.pid)

    async def run_resident(self):
        from official_cli import verify_connection

        conflicts = await powershell(r"""
            $currentSession=(Get-Process -Id $PID).SessionId
            $processes=@(Get-CimInstance Win32_Process | Where-Object SessionId -eq $currentSession)
            $desktop=@($processes | Where-Object {$_.Name -eq 'ChatGPT.exe' -and $_.ExecutablePath -match 'WindowsApps\\OpenAI\.Codex_'})
            $roots=@($desktop | Where-Object {$_.CommandLine -notmatch '--type='} | ForEach-Object {
                $p=Get-Process -Id $_.ProcessId -ErrorAction SilentlyContinue
                if ($p) { [pscustomobject]@{pid=$p.Id; created=$p.StartTime.ToFileTimeUtc().ToString();
                    earliest=([datetimeoffset]$p.StartTime).ToUnixTimeSeconds()} }
            })
            $servers=@($processes | Where-Object {$_.Name -eq 'codex.exe' -and $_.CommandLine -match '\bapp-server\b'})
            [pscustomobject]@{
                shared=@($servers | Where-Object {$_.CommandLine -match '--listen\s+ws'}).Count;
                private=@($servers | Where-Object {$_.ParentProcessId -in $desktop.ProcessId -and $_.CommandLine -notmatch '--listen\s+ws'} | Select-Object -ExpandProperty ProcessId);
                roots=$roots
            } | ConvertTo-Json -Compress
        """)
        counts = json.loads(conflicts)
        if counts["shared"]:
            raise RuntimeError("已检测到共享进程，拒绝启动第二个后端。")
        reopen = bool(counts["private"])
        if reopen:
            if len(counts["roots"]) != 1 or len(counts["private"]) != 1:
                raise RuntimeError("无法唯一确认原桌面和后端，保留当前桌面。")
            root = counts["roots"][0]
            await wait_idle(self.home / "sessions", root["earliest"], None, None)
            await close_original(root["pid"], int(root["created"]))
            await verify_original_exited(counts["private"][0])

        close_job = None
        ready = False
        try:
            await self.start_server()
            close_job = job_for_process(self.server.pid)
            async with serve(self.relay, "127.0.0.1", 0, process_request=self.authorize,
                             max_size=32 * 1024 * 1024) as bridge:
                address = f"ws://127.0.0.1:{bridge.sockets[0].getsockname()[1]}"
                await verify_connection(address, self.secret)
                write_endpoint(f"{address}/{self.secret}")
                status("登录共享后端已就绪", bridge_pid=os.getpid(), backend_pid=self.server.pid)
                ready = True
                if reopen:
                    try:
                        await self.start_desktop(f"{address}/{self.secret}")
                    except OSError:
                        status("登录共享后端已就绪", bridge_pid=os.getpid(), backend_pid=self.server.pid,
                               desktop_launch="failed")
                sample = asyncio.create_task(self.measure_resident())
                try:
                    await self.server.wait()
                finally:
                    sample.cancel()
                    await asyncio.gather(sample, return_exceptions=True)
                raise RuntimeError("官方共享后端已退出。")
        finally:
            if close_job:
                close_job()
            if self.server and self.server.returncode is None:
                try:
                    self.server.terminate()
                except ProcessLookupError:
                    pass
                await self.server.wait()
            if reopen and not ready:
                await verify_original_exited(counts["private"][0])
                await self.start_desktop()

    async def measure_resident(self):
        await asyncio.sleep(20)
        sample = await powershell(f"""
            $ids=@({os.getpid()},{self.server.pid})
            @(Get-CimInstance Win32_PerfFormattedData_PerfProc_Process | Where-Object IDProcess -in $ids |
                ForEach-Object {{ [pscustomobject]@{{pid=$_.IDProcess;
                    private_mib=[math]::Round($_.WorkingSetPrivate/1MB,1)}} }}) | ConvertTo-Json -Compress
        """)
        rows = json.loads(sample)
        if isinstance(rows, list) and len(rows) == 2:
            current = json.loads(STATUS.read_text(encoding="utf-8"))
            phase = current.pop("phase")
            status(phase, **current, working_set=rows)

    async def run(self):
        if not self.alias.exists():
            raise RuntimeError("找不到已注册的官方桌面执行别名。")
        saved = read_endpoint() if self.args.reuse_endpoint else ""
        relay_port = 0
        if self.args.reuse_endpoint:
            address = urlsplit(saved)
            capability = address.path.removeprefix("/")
            if (address.scheme != "ws" or address.hostname != "127.0.0.1" or not address.port
                    or address.username or address.password or address.query or address.fragment
                    or len(capability) != 43 or not all(c.isascii() and (c.isalnum() or c in "-_") for c in capability)):
                raise RuntimeError("原共享地址无法安全恢复。")
            relay_port = address.port
            self.secret = capability
        await wait_idle(self.home / "sessions", self.args.earliest, self.args.rollout, self.args.turn)
        if relay_port:
            with socket.socket() as probe:
                probe.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
                probe.bind(("127.0.0.1", relay_port))
        status("正在正常退出原桌面", old_pid=self.args.pid)
        await close_original(self.args.pid, self.args.created)
        try:
            await verify_original_exited(self.args.old_backend)
            status("正在启动单个正式共享后端")
            await self.start_server()
            async with serve(
                self.relay, "127.0.0.1", relay_port, process_request=self.authorize, max_size=32 * 1024 * 1024,
            ) as bridge:
                endpoint = f"ws://127.0.0.1:{bridge.sockets[0].getsockname()[1]}/{self.secret}"
                signed_in = await self.verify_tui_connection(endpoint)
                write_endpoint(endpoint)
                self.desktop = await self.start_desktop(endpoint)
                await self.keep_serving(endpoint, signed_in)
        except (OSError, RuntimeError, TimeoutError, WebSocketException) as error:
            if self.desktop_alive:
                # A successfully attached desktop may already own user work.
                status("桌面已经接入，验收未完成", detail=str(error), backend_pid=self.server.pid)
                await self.server.wait()
            else:
                if self.desktop and self.desktop.returncode is None:
                    await powershell(
                        f"$p=Get-Process -Id {self.desktop.pid} -ErrorAction SilentlyContinue;"
                        "if($p){$null=$p.CloseMainWindow()}"
                    )
                    try:
                        await asyncio.wait_for(self.desktop.wait(), 12)
                    except TimeoutError:
                        status("回退待处理：新桌面尚未退出", detail=str(error))
                        await self.desktop.wait()
                if self.server and self.server.returncode is None:
                    self.server.terminate()
                    await self.server.wait()
                await verify_original_exited(self.args.old_backend)
                await self.start_desktop()
                status("已恢复原桌面启动方式", detail=str(error))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--open-desktop", action="store_true")
    parser.add_argument("--resident", action="store_true")
    parser.add_argument("--reuse-endpoint", action="store_true")
    parser.add_argument("--verify-cli", action="store_true")
    parser.add_argument("--pid", type=int)
    parser.add_argument("--old-backend", type=int)
    parser.add_argument("--created", type=int)
    parser.add_argument("--earliest", type=float)
    parser.add_argument("--thread")
    parser.add_argument("--turn")
    parser.add_argument("--rollout", type=Path)
    args = parser.parse_args()
    if args.open_desktop:
        from official_cli import verify_connection

        try:
            endpoint = read_endpoint()
            parsed = urlsplit(endpoint)
            if (parsed.scheme != "ws" or parsed.hostname != "127.0.0.1" or not parsed.port
                    or parsed.username or parsed.password or parsed.query or parsed.fragment):
                raise ValueError
            asyncio.run(verify_connection(f"ws://127.0.0.1:{parsed.port}", parsed.path.removeprefix("/")))
        except (OSError, ValueError, KeyError, WebSocketException):
            parser.error("共享后端未就绪。请先恢复共享连接。")
        current = SharedOwner(args)
        command = [str(current.alias)]
        if args.thread:
            command.append(f"codex://threads/{args.thread}")
        subprocess.Popen(command, cwd=ROOT, env=current.desktop_environment(endpoint))
        return
    if not args.resident and not all((args.pid, args.old_backend, args.created, args.earliest, args.thread, args.turn, args.rollout)):
        parser.error("Cutover requires the reviewed original process and active turn identifiers.")
    RUNTIME.mkdir(exist_ok=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p]
    kernel.CreateMutexW.restype = ctypes.c_void_p
    lock = kernel.CreateMutexW(None, False, "Local\\CodexLightSharedOwner")
    if not lock or ctypes.get_last_error() == 183:
        return
    try:
        owner = SharedOwner(args)
        if args.resident:
            asyncio.run(owner.run_resident())
        else:
            asyncio.run(owner.run())
    except (OSError, ValueError, RuntimeError, KeyError, TimeoutError, WebSocketException) as error:
        status("切换未完成", detail=str(error) or type(error).__name__)
        return 1


if __name__ == "__main__":
    sys.exit(main())
