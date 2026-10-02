# CURI 启动与部署打包

本文说明 CURI 的前端、Python 后端如何分别启动，以及如何生成 Windows 安装包和 macOS 桌面应用。

## 项目组成

| 部分 | 目录或文件 | 职责 |
| --- | --- | --- |
| 前端 | `desktop/src/` | React + TypeScript 仪表盘；Vite 负责开发服务器和静态资源构建 |
| 桌面壳 | `desktop/src-tauri/` | Tauri 2 窗口、前端与本地 Python 服务之间的连接、平台安装包 |
| 监控后端 | `curi.py` | 扫描 Codex 会话和 Relay 事件，写入 SQLite，并提供摘要 API |
| Relay | `relay.py` | 可选的本地 OpenAI 兼容转发服务；桌面监控界面不会自动启动它 |

桌面应用通过 `127.0.0.1` 访问本机后端。正式打包时，PyInstaller 会把 Python 后端及其运行时打成 sidecar，再由 Tauri 放进应用包，因此安装后的用户不需要单独安装 Python。

## 只启动 Python 后端

在仓库根目录执行：

```powershell
python curi.py doctor
python curi.py serve
```

macOS/Linux 上如果 Python 命令名是 `python3`，将上面的 `python` 换成 `python3`。服务默认监听 `http://127.0.0.1:8792`，同时启动本地会话扫描，每三秒扫描一次。直接打开该地址会使用现有 Python 内嵌页面。

一次性扫描并输出 JSON 摘要：

```powershell
python curi.py scan
```

诊断默认数据目录：

```powershell
python curi.py doctor
```

## 浏览器开发前端

用两个终端分别启动后端和前端。先在仓库根目录启动后端：

```powershell
python curi.py serve
```

再在另一个终端进入 `desktop/`：

```powershell
cd desktop
npm ci
npm run dev
```

打开 <http://127.0.0.1:1420/>。此模式只启动 React 前端，数据由 `http://127.0.0.1:8792/api/summary` 提供；关闭任一终端都会中断对应服务。

仅构建静态前端文件：

```powershell
cd desktop
npm ci
npm run build
```

输出目录为 `desktop/dist/`。它不是包含 Python 后端的桌面安装包。

## Tauri 桌面开发

需要 Node.js、Python 3.10+ 和 Rust stable 工具链。Windows 还需要 Microsoft C++ Build Tools 和 WebView2；macOS 需要 Xcode 或 Xcode Command Line Tools。

在 `desktop/` 目录执行：

```powershell
npm ci
npm run desktop:dev
```

Tauri 会启动 React 开发服务器，并自动运行仓库根目录的 `curi.py serve --port 0`。后端使用系统分配的空闲本地端口，关闭桌面应用时后端进程也会结束。这个模式不需要预先打包 PyInstaller sidecar，但开发机器需要安装 Python。

## 打包 Windows 应用

在 Windows 机器上打开 PowerShell。先安装 Rust stable、Microsoft C++ Build Tools 和 Node.js；Windows 10/11 通常已包含 WebView2 Runtime。然后执行：

```powershell
cd desktop
python -m pip install -r requirements-build.txt
npm ci
npm run desktop:build
```

`desktop:build` 会先运行 PyInstaller 生成 Windows 后端 sidecar，再构建 React 前端和 Tauri 应用。当前配置的 Tauri bundle target 是 `all`，Windows 安装产物位于：

- NSIS 安装程序：`desktop/src-tauri/target/release/bundle/nsis/*-setup.exe`
- MSI 安装程序：`desktop/src-tauri/target/release/bundle/msi/*.msi`

如果 MSI 构建因 `light.exe` 失败，检查 Windows“可选功能”中的 VBScript 是否启用。也可以在 `desktop/src-tauri/tauri.conf.json` 中把 bundle targets 改成只生成 `nsis`。

## 打包 macOS 应用

在 macOS 机器上安装 Xcode Command Line Tools、Rust stable、Node.js 和 Python 3.10+，然后在终端执行：

```bash
cd desktop
python3 -m pip install -r requirements-build.txt
npm ci
npm run desktop:build
```

产物位于 `desktop/src-tauri/target/release/bundle/`，包含 macOS `.app` 应用包和 DMG 安装镜像。当前 sidecar 脚本按本机 CPU 架构构建；Apple Silicon 和 Intel Mac 需要分别在对应架构环境中构建。

本地试用可以使用未签名构建。向其他用户分发时，应使用 Apple Developer 证书进行代码签名和 notarization；否则 macOS 可能提示应用无法验证。Windows 对外分发也建议签署安装程序，减少 SmartScreen 警告。

## 数据位置与运行边界

默认读取当前系统用户的 Codex 目录：

- Windows：`%USERPROFILE%\.codex\sessions`
- macOS/Linux：`~/.codex/sessions`

SQLite 数据库默认位于用户目录下的 `.curi/curi.sqlite3`；Relay 事件默认位于 `.curi/relay-events.jsonl`。桌面应用只启动监控服务，不会启动 Relay 转发服务，也不会自动配置 Codex 的 API 地址。

单独运行 Relay 时，在仓库根目录执行：

```powershell
python curi.py relay --upstream https://api.example.com/v1
```

如需同时运行监控面板和 Relay，可以使用：

```powershell
python curi.py serve --upstream https://api.example.com/v1
```

更多参数可查看：

```powershell
python curi.py serve --help
python curi.py relay --help
```

## 官方构建参考

- [Tauri 前置环境](https://v2.tauri.app/start/prerequisites/)
- [Tauri Windows 安装包](https://v2.tauri.app/distribute/windows-installer/)
- [Tauri macOS DMG](https://v2.tauri.app/distribute/dmg/)
- [Tauri macOS 签名](https://v2.tauri.app/distribute/sign/macos/)
- [Tauri Sidecar](https://v2.tauri.app/develop/sidecar/)
