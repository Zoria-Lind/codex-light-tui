"""PowerShell entry for the installed official Codex executable."""

import asyncio
import json
import subprocess
import sys
from urllib.parse import urlsplit

from websockets.asyncio.client import connect
from websockets.exceptions import WebSocketException

from backend import codex_environment, codex_executable
from shared_state import read_endpoint


TOKEN_ENV = "CODEX_LIGHT_CLI_BEARER"
SHARED_COMMANDS = {"resume", "agents", "queue", "fork", "archive", "unarchive", "delete"}


async def verify_connection(address, token):
    async with connect(address, additional_headers={"Authorization": f"Bearer {token}"},
                       proxy=None, open_timeout=4, close_timeout=1) as socket:
        async with asyncio.timeout(5):
            await socket.send(json.dumps({"id": 1, "method": "initialize", "params": {
                "clientInfo": {"name": "codex_light_cli_probe", "title": "Codex CLI connection check", "version": "0.1.0"},
            }}))
            async for raw in socket:
                message = json.loads(raw)
                if message.get("id") == 1:
                    if "result" in message:
                        return
                    break
            raise ValueError("共享入口尚未连通官方后端")


def main(args):
    env = codex_environment()
    executable = codex_executable(env)
    if not args:
        args = ["resume", "--all"]
    elif args == ["new"]:
        args = []
    elif args[0] == "--local":
        return subprocess.call([executable, *(args[1:] or ["resume", "--all"])], env=env)

    if not args or args[0] in SHARED_COMMANDS or args[0] in {"-i", "--image", "-m", "--model"}:
        if "--help" not in args and "-h" not in args:
            try:
                endpoint = urlsplit(read_endpoint())
                token = endpoint.path.removeprefix("/")
                if (endpoint.scheme != "ws" or endpoint.hostname != "127.0.0.1" or not endpoint.port
                        or endpoint.username or endpoint.password or endpoint.query or endpoint.fragment
                        or not token or "/" in token):
                    raise ValueError("原共享地址不可用")
                address = f"ws://127.0.0.1:{endpoint.port}"
                asyncio.run(verify_connection(address, token))
            except (OSError, ValueError, KeyError, TimeoutError, WebSocketException):
                print("Codex 共享后端未连接。请先恢复共享连接。需单独运行时可用 codex --local。", file=sys.stderr)
                return 1
            if any(arg.split("=", 1)[0] in {"--remote", "--remote-auth-token-env"} for arg in args):
                print("共享模式使用当前会话的连接，请移除自定义 --remote 参数。", file=sys.stderr)
                return 2
            env[TOKEN_ENV] = token
            args = [*args, "--remote", address, "--remote-auth-token-env", TOKEN_ENV]
    return subprocess.call([executable, *args], env=env)


if __name__ == "__main__":
    try:
        raise SystemExit(main(sys.argv[1:]))
    except (OSError, RuntimeError) as error:
        print(error, file=sys.stderr)
        raise SystemExit(1) from None
