# 桌面同步与接力（实验功能）

官方桌面和 TUI 可以连接同一个本机 Codex App Server，打开同一条本地聊天。即使退出桌面窗口，只要共享后端仍在运行，TUI 就能继续接收共享任务的输出。普通 ChatGPT 聊天和仅在云端可见的记录仍需在官方界面中打开，两个界面的侧栏和排序也可能不同。

## 1. 准备

- 按 [README](../README.md#安装和启动) 完成安装，确认可以独立打开 TUI，并在官方 Codex CLI 完成登录。此流程基于 Codex CLI 0.171.0 和提供 `chatgpt-desktop.exe` 应用执行别名的 Windows 官方桌面版。桌面或 CLI 更新后，建议先用测试聊天验证。
- 等当前工作结束，退出 TUI，并通过官方桌面的菜单完整退出桌面应用。先退出桌面可以避免旧窗口接管新启动请求，让你更容易确认走的是共享连接。若本机已有共享后端在运行，请先停用由你管理的旧实例，或继续使用它原来配置好的客户端目录。
- 打开两个 PowerShell 标签页，都进入这份源码的根目录。以下命令只为当前标签页设置环境，不需要管理员权限。

在两个标签页各运行一次下面的设置。若你已有自定义 Codex 存储，两个标签页的 `CODEX_HOME` 都要指向同一个现有目录。

```powershell
if (-not $env:CODEX_HOME) {
    $env:CODEX_HOME = [Environment]::GetEnvironmentVariable('CODEX_HOME', 'User')
}
if (-not $env:CODEX_HOME) {
    $env:CODEX_HOME = Join-Path $env:USERPROFILE '.codex'
}
$env:CODEX_HOME = [Environment]::ExpandEnvironmentVariables($env:CODEX_HOME)
Remove-Item Env:CODEX_LIGHT_SERVER_URL, Env:CODEX_LIGHT_SERVER_TOKEN -ErrorAction SilentlyContinue
```

如果官方桌面应用执行别名不可用，可在 Windows 的「应用执行别名」中核对，或检查官方桌面是否已安装：

```powershell
Test-Path (Join-Path $env:LOCALAPPDATA 'Microsoft/WindowsApps/chatgpt-desktop.exe')
```

## 2. 启动共享后端

在第一个标签页运行，运行期间保持这个标签页打开：

```powershell
.\.venv\Scripts\python.exe -X utf8 .\shared_owner.py --resident
```

命令在前台维持后端，通常不会持续打印状态。它会在回环接口上启动受认证保护的连接，并把连接记录写入 `.shared-runtime`，由当前 Windows 用户加密。用第二个标签页查看状态：

```powershell
Get-Content .\.shared-runtime\status.json -Encoding UTF8 |
    ConvertFrom-Json | Select-Object phase, bridge_pid, backend_pid
```

等 `phase` 显示「登录共享后端已就绪」再继续。若状态文件还没生成，稍等几秒重试。若进程已退出或提示「切换未完成」，可在本机查看 `status.json` 的 `detail`，它可能包含本机路径。共享连接记录和完整状态文件应留在本机。

## 3. 打开双端并验证

在第二个标签页依次运行。桌面端应用窗口打开后，TUI 会占用当前终端。

```powershell
.\open-shared-desktop.cmd
.\start.cmd
```

1. 确认 TUI 状态显示「共享 Codex 后端」。用 TUI 的左栏打开一条本地测试聊天，或新建一条并发送第一条消息。
2. 等回复完成后，点 TUI 顶部的「桌面 ↗」。官方桌面应打开同一条聊天并显示同一轮消息。若窗口未跳转，可以在桌面侧栏手动打开刚才的测试聊天。
3. 从桌面发送一条容易辨认的测试消息，等待回复结束。在 TUI 查看同一条聊天，必要时按 `F5` 刷新。双方能看到这两轮消息，就验证了这条本地聊天可以双向续聊。

同一条聊天每次只从一端发送。两个界面都可以用来查看聊天，写入占用由官方后端处理。

### 可选：从桌面一键切到 TUI

先退出正在测试的 TUI，保留第一个标签页里的共享后端和已通过共享入口打开的桌面。在第二个标签页执行：

```powershell
.\show-desktop-button.cmd
```

切回官方桌面，把要接力的本地聊天放在前台，再点「切到 TUI」。按钮会核对当前聊天，等新的 TUI 接好该会话后才请求桌面正常退出。当聊天标题无法唯一确认、任务仍在运行或共享连接不可用时，原桌面会保留。可以等回复完成再试，或直接在 TUI 里打开同一条聊天。右键浮动按钮可关闭它。

## 4. 排查与退出共享模式

| 现象 | 处理 |
|---|---|
| TUI 显示连接失败 | 检查第一个标签页是否仍在运行，以及状态中的进程是否仍存在。启动共享后端后按 `F5` 重连。重启电脑后需要重新运行第 2 步。 |
| 提示后端已存在 | 当前 Windows 登录会话已有共享后端。使用对应的原客户端目录，或先正常停止由你管理的旧实例，再启动此副本。 |
| 桌面还能看到一些额外聊天 | 先用一条确定是本地的测试聊天验证。普通 ChatGPT 和云端内容仍在官方界面查看。 |
| 使用自建 WebSocket 后端 | TUI 支持 `CODEX_LIGHT_SERVER_URL` 和可选的 `CODEX_LIGHT_SERVER_TOKEN`，地址必须是本机回环接口。桌面需从同一外部启动器打开。以上桌面接力脚本使用的是本教程生成的连接记录。 |

回到独立模式前，等两端任务结束，退出桌面和 TUI，然后回到第一个标签页按 `Ctrl+C` 停止本次启动的共享后端。将这份副本中的连接记录改名保留，然后重新启动：

```powershell
if (Test-Path .\.shared-runtime\endpoint.json) {
    Rename-Item -LiteralPath .\.shared-runtime\endpoint.json `
        -NewName ('endpoint.disabled-' + [guid]::NewGuid().ToString('N') + '.json')
}
Remove-Item Env:CODEX_LIGHT_SERVER_URL, Env:CODEX_LIGHT_SERVER_TOKEN -ErrorAction SilentlyContinue
.\start.cmd
```

此时 TUI 会为自己启动独立 App Server。官方桌面可再通过 Windows 开始菜单正常打开。回退操作只涉及此副本的连接记录，会话仍保存在原来的 `CODEX_HOME`。
