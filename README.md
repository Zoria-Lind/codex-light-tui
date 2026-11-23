# Codex Light TUI

当前版本 **0.1.0**

面向 Windows 的轻量终端客户端。它使用 Textual 显示界面，由本机安装的官方 Codex App Server 处理聊天、任务和审批。

## 安装和启动

需要 Windows、Python 3.11 或更高版本，以及已安装并完成登录的官方 Codex CLI。原型在 Codex CLI 0.171.0 上验证过。其他版本的项目接口与桌面接力可能有差异。

在源码目录中打开 Windows Terminal，运行：

```powershell
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\start.cmd
```

客户端会尝试定位官方 `codex.exe`，包括 npm 启动器对应的原生程序。如果搜索不到，可以在当前 PowerShell 会话中指定它。将示例文件名替换为相对于当前目录的实际可执行文件位置：

```powershell
$env:CODEX_LIGHT_EXE = (Resolve-Path '.\codex.exe').Path
.\start.cmd
```

首次运行默认启动独立 App Server 进程，使用当前用户的 Codex 配置和会话存储。需要测试配置时，可仅为当前 PowerShell 会话指定单独的 `CODEX_HOME`。退出前端使用 `Ctrl+Q`。

## 日常操作

| 操作 | 入口 |
|---|---|
| 浏览、搜索和新建本地项目或聊天 | 左栏，`Ctrl+B` 收起，`Ctrl+F` 搜索 |
| 并行跑任务 | 在一个聊天发出任务后切换到另一个聊天。停止按钮只作用于当前聊天 |
| 回答问题和处理审批 | 打开左栏标记「待回答」或「待审批」的聊天 |
| 发送、换行与刷新 | `Enter` 发送，`Shift+Enter` 或 `Ctrl+J` 换行，`F5` 刷新或重连 |
| 查看工具输出及 diff | 点击聊天中的折叠行，命令输出可继续展开 |
| 粘贴图片 | 点击「粘贴图」或按 `Ctrl+Alt+V`。终端转发 `Ctrl+V` 时也可使用它 |
| 选择模型与思考强度 | 点击右下角的模型和强度。权限设置在左下角 |

项目列表取自本地 Codex。官方桌面还能展示部分云端或普通 ChatGPT 会话。这些会话仍在官方界面中打开。多个客户端打开同一条本地聊天时，由官方后端处理写入占用。

## 实验功能

想在官方桌面和 TUI 之间续聊同一条本地会话，请看[桌面同步与接力教程](docs/desktop-sync.md)。教程覆盖首次启动、双端验证、可选的「切到 TUI」按钮和回退。常规安装没有自启动步骤。

## 验证

```powershell
.\start.cmd --help
.\.venv\Scripts\python.exe -X utf8 test_parallel.py
```

回归测试在模拟后端上覆盖并行发送、草稿和附件隔离、后台提问和审批，以及新建项目。运行时生成的界面状态、粘贴图片和共享连接记录保存在当前副本目录，已列入 `.gitignore`。

预览版已在新建的 Windows Python 3.11 虚拟环境中安装依赖，并用空白 Codex 配置启动官方 App Server、读取空会话列表。此验证未提交模型请求。

## 许可证

本项目是独立社区客户端，采用 [MIT 许可证](LICENSE)。Codex CLI 和 App Server 由用户自行安装。
