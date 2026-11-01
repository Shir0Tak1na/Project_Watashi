# v1.0.0-rc1 人工验收 / Manual acceptance

状态：**未验收（pending）**。本文件是给人工执行的清单，不是通过证明。
自动测试结果见 [docs/RELEASE.md](RELEASE.md)；安装说明见 [docs/INSTALL.md](INSTALL.md)。

> 规则：只有实际执行并留下证据的条目才勾选。没有执行或无法执行的条目写
> **未验**，不得写成“通过”。`skipped` 不等于 `passed`。

## 0. 产物与前置 / Artefacts

- ZIP：`dist/ProjectWatashi-1.0.0-rc1-win64.zip`
- 校验：`dist/SHA256SUMS.txt`
- 安装方式：解压后运行 `install.ps1`；**per-user**，不需要管理员，不触发 UAC。
- 所有证据（截图、日志、报告）放到 `build-rc/acceptance/<日期>/` 或本文件
  表格的“证据”栏中；不要提交用户数据或证书。

### 0.1 环境记录

| 项 | 记录 |
| --- | --- |
| 验收人 / 日期 | |
| 机器 / Windows 版本 | |
| 用户类型（普通 / 管理员） | |
| 完整性级别（`whoami /groups`） | |
| 是否有通知区 | |

## 1. Medium 桌面（当前开发机） / Medium-integrity desktop

前置：普通用户 PowerShell，**不要**“以管理员身份运行”。

| # | 步骤 | 期望 | 证据 | 状态 |
| --- | --- | --- | --- | --- |
| M1 | 解压 ZIP 到临时目录，检查无 `.bin/.onnx/.pt`、无 `models/`、无 `.pdb` | 全部为空 | ZIP 列表截图 / `unzip-check` 路径 | ☐ 未验 |
| M2 | `install.ps1 -Prefix <tmp>\app -UninstallKey HKCU:\Software\ProjectWatashi\AcceptTest -ShortcutDir <tmp>\menu` | 退出码 0；输出 installed + user 路径 | 终端日志 | ☐ 未验 |
| M3 | 检查 HKCU 卸载项：`Get-ItemProperty HKCU:\Software\ProjectWatashi\AcceptTest` | `DisplayName=Project Watashi`、`InstallLocation` 指向前缀 | 截图/注册表导出 | ☐ 未验 |
| M4 | 检查开始菜单快捷方式 `<tmp>\menu\Project Watashi.lnk` | 存在，目标为 `watashi_ui.exe` | 截图 | ☐ 未验 |
| M5 | `watashi_ui.exe --settings --capture synthetic --ocr fake --duration 60` | 设置页打开；悬浮窗出现；60 s 后自行退出 0 | `--report` JSON + 截图 | ☐ 未验 |
| M6 | `watashi_ui.exe --version` | `Project Watashi 1.0.0-rc1` + commit + `build Release` | 终端输出 | ☐ 未验 |
| M7 | 检查 `%LOCALAPPDATA%\ProjectWatashi\logs\watashi.log` | 有 start/exit 行；无字幕原文；无 `[error]` | 日志文件 | ☐ 未验 |
| M8 | 写入 `user\user.json` 后运行 `uninstall.ps1` | payload/快捷方式/HKCU 项移除；`user\user.json` 保留 | 终端日志 + `Test-Path` | ☐ 未验 |
| M9 | 卸载后 `-RemoveUserData` 单独复验（可选） | `user\` 删除，前缀在无残留时删除 | 终端日志 | ☐ 未验 |

## 2. 干净 Win11 VM / Clean VM

前置：全新 Win11 VM，**普通用户**，不安装管理员工具；记录是否出现 UAC 提示。

| # | 步骤 | 期望 | 证据 | 状态 |
| --- | --- | --- | --- | --- |
| V1 | 解压 ZIP，运行 `install.ps1` | 无 UAC、无管理员；安装到 `%LOCALAPPDATA%\ProjectWatashi` | 录屏/截图 | ☐ 未验 |
| V2 | `watashi_ui.exe --settings` + synthetic 60 s | 同上，窗口正常、DPI 正确 | 报告 JSON | ☐ 未验 |
| V3 | `watashi_ui.exe --version` | 1.0.0-rc1 + commit + Release | 终端输出 | ☐ 未验 |
| V4 | 检查日志/`user\`/卸载项/快捷方式 | 同 M3/M4/M7 | 文件清单 | ☐ 未验 |
| V5 | 运行 `uninstall.ps1` | 清理 payload/快捷方式/HKCU；保留 `user\` | 终端日志 | ☐ 未验 |

> 该项在仓库当前状态下是 **未验证**：没有可用的干净 VM。见 RELEASE.md。

## 3. 模型 / Models

不需要模型即可完成安装与 UI 验收；以下条目需要网络时才执行 `--fetch`。

| # | 步骤 | 期望 | 证据 | 状态 |
| --- | --- | --- | --- | --- |
| C1 | `watashi_fetch.exe --check --dest <user>`（未下载模型） | 退出码 **1**；打印缺失清单；不联网 | 终端输出 | ☐ 未验 |
| C2 | `watashi_fetch.exe --fetch --offline --dest <user>` | 拒绝全部 http；退出码 1；不发起网络请求 | 终端输出 | ☐ 未验 |
| C3 | （另行确认网络与体积后）`watashi_fetch.exe --fetch --dest <user>` | 下载后 SHA-256/字节数校验通过，原子改名 | 报告/文件哈希 | ☐ 未验 |
| C4 | 检查安装包内仍无权重 | 无 `.bin/.onnx/.pt/.pth/.safetensors`，无 `models/` | ZIP 列表 | ☐ 未验 |
| C5 | 阅读许可 | 代码 MIT；NLLB CC-BY-NC-4.0（非商业）；Whisper MIT；PP-OCRv4 Apache-2.0 | `licenses/MODEL_LICENSES.md` | ☐ 未验 |

## 4. 托盘 / Tray

仅在有正常通知区的桌面执行；若 `Shell_NotifyIcon` 失败，记录为 known issue，
不要勾选通过。

| # | 步骤 | 期望 | 证据 | 状态 |
| --- | --- | --- | --- | --- |
| T1 | 启动 `watashi_ui`，观察 stderr / 通知区 | 打印 `tray: icon added`，图标可见 | 截图 + stderr | ☐ 未验 |
| T2 | 右键托盘图标 → 菜单 | 菜单出现（Pause/Hide/Exit） | 截图 | ☐ 未验 |
| T3 | 点击 Exit | 进程退出，`--report` 的 `exit_reason=tray-exit` | 报告 JSON | ☐ 未验 |

> 已知问题：部分会话/宿主上 `Shell_NotifyIcon(NIM_ADD)` 返回 error 5，图标不可用；
> 此时 T1–T3 记为 **known issue（未验）**，不影响其他条目。

## 5. 长稳 / Soak

短测由 CTest 覆盖；10 分钟与 8 小时必须手工启动并保存报告。

```powershell
# 10 分钟 smoke
powershell -NoProfile -ExecutionPolicy Bypass -File tools\soak.ps1 `
  --duration 600 --mode ui --report build-rc\soak-10m.json

# 8 小时（启动后记录 PID；未完成前状态只能是“进行中”）
powershell -NoProfile -ExecutionPolicy Bypass -File tools\soak.ps1 `
  --duration 28800 --mode ui --report build-rc\soak-8h.json
```

报告字段：`mode`、`requested_duration_seconds`、`elapsed_seconds`、
`handles_start` / `handles_end`、`memory_mib_start/end/peak`、`exit_code`、
`crashed`、`timed_out`、`log_errors`、`pass`、`failure_reason`、`started_at`、
`finished_at`。

通过标准（建议）：

- `pass=true`；
- `exit_code=0`，`crashed=false`，`timed_out=false`；
- `elapsed_seconds >= requested_duration_seconds - 容差`；
- `log_errors=0`；
- handles 不应持续单调增长（记录数值，不做硬性门槛）；
- 工作集峰值记录在案，用于对比。

| # | 步骤 | 期望 | 证据 | 状态 |
| --- | --- | --- | --- | --- |
| S1 | 10 分钟 UI smoke | `pass=true` | `build-rc/soak-10m.json` | ☑ PASS（自动 2026-11-01：602.5s，exit 0，log_errors 0） |
| S2 | 8 小时 UI soak | 启动后记录 PID；运行中只能标“进行中”，完成且 `pass=true` 才可标 PASS | `build-rc/soak-8h.json` + PID 5224 | 进行中（2026-11-01 20:43 启动，报告尚未生成） |

## 6. 签名 / Signing

| # | 步骤 | 期望 | 证据 | 状态 |
| --- | --- | --- | --- | --- |
| G1 | 无证书运行 `sign.ps1` | 打印 `UNSIGNED`，不冒充已签名 | 终端输出 | ☐ 未验 |
| G2 | 有证书时 `WATASHI_SIGN_CERT=...` 运行 | `SIGNED` 且 signtool verify 通过；证书不入库 | 终端输出 | ☐ 未验 |

## 7. 汇总 / Summary

| 组 | 结论 | 备注 |
| --- | --- | --- |
| Medium 桌面（M） | 未验 | |
| 干净 VM（V） | 未验 | 无可用 VM |
| 模型（C） | 未验 | |
| 托盘（T） | 未验 | 需真实通知区/菜单点击 |
| Soak（S） | 10m PASS（自动）；8h 进行中 | 报告路径见上 |
| 签名（G） | unsigned | |
