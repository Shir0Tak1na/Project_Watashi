# v1.0.0-rc1 人工验收 / Manual acceptance

状态日期：**2026-11-11**。
结论：**RC1 的 B 验收已完成**（Medium 桌面、干净 Win11 VM、控制面、安装/卸载）。
**最终 v1.0.0 已发布并 push（2026-11-12，tag `v1.0.0` deref `a0ecccd`）**：本文档的
RC1 验收结果作为最终 1.0.0 的**验收输入**沿用；历史结果不改写。最终包、clean
build 与发布记录见 [docs/RELEASE.md](RELEASE.md)。

> 规则：只有实际执行并留下证据的条目才写 PASS。没有执行或无法执行的条目写
> **未验**，不得写成“通过”。skipped 不等于 passed。
>
> 本次验收对象是 `build-rc/stage-vcomp.zip`（含 vcomp140.dll），不是旧 RC1 ZIP。
> 旧 RC1 ZIP 已归档到 `build-rc/archive/rc1/`。
>
> 最终 1.0.0 包（`dist/ProjectWatashi-1.0.0-win64.zip`）与验收包的 payload
> 文件列表一致（自动 ZIP 审计核对），差异仅为版本字符串（1.0.0-rc1 → 1.0.0）
> 与 SBOM 日期；因此本文档的人工结果对最终包有效。

## 0. 产物 / Artefacts

| 项 | 值 |
| --- | --- |
| 验收包 | `build-rc/stage-vcomp.zip` |
| 字节数 | 16,943,345 |
| SHA-256 | `c117554c7fe2ddc79c38b3789d63a32407f7abada423f790df33427935da8ee2` |
| 版本输出 | `Project Watashi 1.0.0-rc1`，commit `b524ce0c3c18`，build Release |
| 旧 RC1 包 | `build-rc/archive/rc1/ProjectWatashi-1.0.0-rc1-win64.zip`，SHA-256 `a2ec862c...` |
| 最终 v1.0.0 | ✅ 已生成：`dist/ProjectWatashi-1.0.0-win64.zip`，16,483,547 字节，SHA-256 `da6f0c99...`（完整值见 RELEASE §1）；payload 列表与验收包一致，版本输出 `Project Watashi 1.0.0 / commit 7a2c1a0f2eb4 / Release` |

安装方式：解压后运行 `install.ps1`；per-user，不需要管理员，不触发 UAC。
证据目录：`build-rc/acceptance/2026-11-11/`（本机可审计的控制面报告）；
VM 内的证据由用户确认，截图未入库。

## 0.1 环境记录 / Environment

| 项 | 记录 |
| --- | --- |
| 日期 | 2026-11-11 |
| 主机 | Windows 10 Home China（build 26300），Medium 完整性，无可用通知区 |
| 主机通知区 | `Shell_TrayWnd`/`TrayNotifyWnd` 存在，`ToolbarWindow32=0`；`Shell_NotifyIcon(NIM_ADD)` 返回 error 5；最小 NotifyIcon 探针同样失败 |
| VM | VirtualBox 7.2.22；Windows 11 Enterprise Evaluation build 26100 |
| VM 安装方式 | **BIOS + LabConfig 绕过 TPM/SecureBoot**；VirtualBox 7.2 EFI 在 DXE_AP 阶段黑屏，未完成 UEFI/TPM 安装验收 |
| VM 用户 | 标准用户 `watashi-test`（管理员 `tester` 仅用于建账户） |
| VM 安装包 | `stage-vcomp.zip`，含 `vcomp140.dll` |

## 1. Medium 桌面 / Medium desktop

| # | 步骤 | 结果 | 证据 |
| --- | --- | --- | --- |
| M1 | 解压验收包，检查无权重/无 `models/`/无 `.pdb` | ✅ PASS（用户确认） | 用户截图；ZIP 审计 |
| M2 | `install.ps1` per-user 安装，无管理员/UAC | ✅ PASS（用户确认） | 用户终端输出 |
| M3 | HKCU 卸载项 | ✅ PASS（用户确认） | 用户截图 |
| M4 | 开始菜单快捷方式 | ✅ PASS（用户确认） | 用户截图 |
| M5 | 设置页 / synthetic 运行 | ✅ PASS（用户确认） | 用户确认 |
| M6 | `--version` = 1.0.0-rc1 + commit + Release | ✅ PASS | 终端输出 |
| M7 | 日志无字幕原文、无 error | ✅ PASS（用户确认） | 日志 |
| M8 | `uninstall.ps1` 保留 `user\` | ✅ PASS（用户确认） | `Test-Path` |
| M9 | `-RemoveUserData` 单独复验 | 未验（可选） | — |

## 2. 干净 Win11 VM / Clean VM

| # | 步骤 | 结果 | 证据 |
| --- | --- | --- | --- |
| V1 | 标准用户安装，无管理员/UAC | ✅ PASS（用户确认） | VM 终端输出 |
| V2 | 控制窗 / synthetic 运行 | ✅ PASS（用户确认） | VM 内 `control-report.json` |
| V3 | `--version` = 1.0.0-rc1 + commit + Release | ✅ PASS | VM 终端输出 |
| V4 | 日志 / `user\` / 卸载项 / 快捷方式 | ✅ PASS（用户确认） | VM 截图 |
| V5 | `uninstall.ps1`，保留 `user\` | ✅ PASS（用户确认） | 用户 `Test-Path` 输出 |

**VM 诚实记录**：此 VM 是 **BIOS + TPM/SecureBoot 绕过**安装的，不是 UEFI/安全启动
验收；应用功能验收有效，但“UEFI/TPM 干净安装”仍是未验证项。

## 3. 控制面 / Control surface

| # | 步骤 | 结果 | 证据 |
| --- | --- | --- | --- |
| T1 | `--control auto/window` 在 tray 不可用时显示 `watashi control` | ✅ PASS | `build-rc/acceptance/2026-11-11/control-report.json`；`control_surface=window`、`tray.added=false` |
| T2 | Pause/Resume 按钮 | ✅ PASS（用户确认） | report `commands.last_cmd`/`results` |
| T2b | Hide/Show 按钮 | ✅ PASS（用户确认） | 用户截图/确认 |
| T3 | Exit 按钮 | ✅ PASS | report `exit_reason=control-exit`、`exit_code=0`、`subtitles=131` |
| T4 | Ctrl+Alt+Q | 未验（VM 中 2 个热键注册失败，未单独确认） | — |
| T5 | report 字段与实际一致 | ✅ PASS | `control_surface=window`、`tray.added=false`、`exit_reason=control-exit` |

> `stream.exit_code=-1` 是 `ChildStream.stop()` 主动停止的哨兵值，不是失败；
> 以 UI 的 `exit_code=0` 和 `exit_reason` 为准。
>
> 托盘：本机/VM 均未取得可用 tray；**不要求 tray PASS**。控制面（ControlWindow）
> 已 PASS。tray 实点仍是未验证项。

## 4. 安装 / 卸载

| 项 | 结果 |
| --- | --- |
| 标准用户 install，无 UAC | ✅ PASS（VM + Medium） |
| HKCU 卸载项 / 开始菜单 | ✅ PASS |
| payload + licenses + config + vcomp | ✅ PASS |
| uninstall 移除 payload / 快捷方式 / HKCU | ✅ PASS |
| 默认保留 `user\` | ✅ PASS（`Test-Path ...\user` = True） |

## 5. 模型 / Models

未执行 `--fetch`（需要网络/体积确认）；以下为未验，不是失败：

| # | 步骤 | 状态 |
| --- | --- | --- |
| C1 | `watashi_fetch --check` 缺模型返回 1 | ✅ 通过（2026-11-12）：exit 1，14 个缺失项 |
| C2 | `--offline` 拒绝网络 | ✅ 通过（2026-11-12）：13 个 http 资产被拒 |
| C3 | 实际下载与哈希/原子改名 | ⛔ 未验：未联网执行 `--fetch`；`ppocr_keys_v1.txt` 也不在 lock 中 |
| C4 | 包内无权重 | ✅ PASS（ZIP 审计） |
| C5 | 许可阅读 | ✅ PASS（`licenses/MODEL_LICENSES.md`） |

## 6. 长稳 / Soak

| # | 项目 | 结果 |
| --- | --- | --- |
| S1 | 10 分钟 UI smoke | ✅ PASS：602.5 s，exit 0，log_errors 0 |
| S2 | 8 小时 UI soak | ✅ PASS：28801.5 s，exit 0，crashed=false，timed_out=false，handles 169→169，内存 15.0→13.8 MiB（peak 16.4），log_errors 0，stderr_errors 0 |

报告：`build-rc/soak-10m.json`、`build-rc/soak-8h.json`。

## 7. 签名 / Signing

- 当前发布：**unsigned**。
- 证书签名未验证；`sign.ps1` 只在有证书时签名。

## 8. 汇总 / Summary

| 组 | 结论 | 备注 |
| --- | --- | --- |
| Medium 桌面（M） | ✅ PASS（用户确认） | 无可用 tray |
| 干净 VM（V） | ✅ PASS（用户确认） | BIOS + TPM/SecureBoot 绕过；UEFI 未验 |
| 控制面（T） | ✅ PASS | T1/T2/T2b/T3/T5；T4 未验 |
| 安装/卸载 | ✅ PASS | `user\` 保留 |
| 模型（C） | 未验 | 未执行网络下载 |
| Soak（S） | ✅ PASS | 10m + 8h |
| 签名（G） | unsigned | 未验 |

**B 结论：通过。**

**最终 v1.0.0：已发布并 push（2026-11-12，tag `v1.0.0`）。** 本文 RC1 验收即其验收输入；
未验项（T4 热键实按、UEFI/TPM、tray 实点、模型下载、签名）继续列为未验，
见 [docs/RELEASE.md](RELEASE.md) §9。

## 9. 发布后验证 / Post-release checks

以下不属于 RC1 的 B 验收，但属于最终 1.0.0 的发布后 P0：

| # | 项目 | 状态（2026-11-12） |
| --- | --- | --- |
| R1 | push `main` + `v1.0.0` tag，远端可查 | ✅ 完成：`4ea7a2a` / tag deref `a0ecccd` |
| R2 | ZIP + SHA256SUMS 备份到第二个盘/外部存储 | ✅ 完成：`D:\WatashiRelease\v1.0.0\`，`-c` OK |
| R3 | `watashi_fetch --check` 缺模型返回 1 | ✅ 通过：exit 1，14 个缺失项 |
| R4 | `--offline` 拒绝网络 | ✅ 通过：exit 1，13 个 http 资产被拒 |
| R5 | 实际 `--fetch`（模型约 1.1 GB） | ⛔ 未验：本轮未联网；`ppocr_keys_v1.txt` 不在 lock |
| R6 | 真实 OCR + NMT 运行（非 fake/corpus） | ⚠️ 功能通过 / 报告字段不通过：`watashi control` → `瓦西控制`，但 `backend`/`trace` 写死为 fake |
| R7 | 最终 1.0.0 ZIP 干净安装/运行/卸载 | ⚠️ 安装/运行通过；卸载在 `%LOCALAPPDATA%` 被本机拦截，备用 prefix 通过 → 真实目录卸载未验 |

详见 [docs/POST-V1.0.md](POST-V1.0.md)。P0 通过前不开始 v1.1，
也不把 tray/UEFI/签名/复现性当作新功能开发。
