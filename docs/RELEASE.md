# 发布验收清单 / Release checklist

状态日期：**2026-11-11**（RC1 人工验收完成；最终 v1.0.0 待 Stage D）。

## 0. 当前状态 / Status

| 项目 | 状态 |
| --- | --- |
| CTest（当前 HEAD 9be995d） | **318/318 通过**（2 个 installer 在 Low IL 宿主 skip） |
| 10 分钟 UI soak | **PASS**：`build-rc/soak-10m.json`，602.5 s，exit 0，log_errors 0 |
| 8 小时 UI soak | **PASS**：`build-rc/soak-8h.json`，28801.5 s，exit 0，handles 169→169，内存 15.0→13.8 MiB（peak 16.4），log_errors 0 |
| Medium 桌面安装/运行/卸载 | **PASS（用户 2026-11-11）** |
| 干净 Win11 VM 安装/运行/卸载 | **PASS（用户 2026-11-11）**；VM 为 **BIOS + LabConfig 绕过 TPM/SecureBoot**，不是 UEFI 验收 |
| 控制面 | **PASS**：`control_surface=window`，Pause/Hide/Exit，`exit_reason=control-exit` |
| 托盘 | **不可用**（本机/VM 均无正常通知区）；best-effort，不是 release blocker |
| 模型下载 | 未执行 `--fetch`；`--check` 未验 |
| 签名 | **unsigned**；无证书 |

> **skipped ≠ passed**：CTest 中 `installer_install` /
> `installer_remove_user_data` 在 Low IL 宿主以 77 退出并显示 skipped；
> 人工安装验收已由用户完成，但自动 skip 不写成 pass。

## 1. RC1 验收产物 / RC1 acceptance artefact

- `build-rc/stage-vcomp.zip`，16,943,345 字节
- SHA-256：`c117554c7fe2ddc79c38b3789d63a32407f7abada423f790df33427935da8ee2`
- 含 `vcomp140.dll`（CTranslate2/OpenMP 运行时；干净 VM 必需）
- 旧 RC1 ZIP 已归档：`build-rc/archive/rc1/ProjectWatashi-1.0.0-rc1-win64.zip`
  （SHA-256 `a2ec862c4d4fd5fc59f4954b9ceb4d21ce7b701054be1f9ed4a57120b857f932`）
- **最终 `dist/ProjectWatashi-1.0.0-win64.zip` 尚未生成**，由 Stage D 负责。

## 2. clean-room / reproducibility

- 旧 RC1 clean-room：`git archive` → `build-rc/clean-build`，313/313 通过；
  日志已归档到 `build-rc/archive/rc1/clean-ctest.log`。
- 当前 HEAD 已加入 control surface 和 vcomp，测试为 **318/318**；最终包必须
  从 clean build 重新生成。
- **不是逐字节可复现**：此前对比有 9 个文本文件 CRLF/LF 不同、4 个二进制
  同尺寸不同哈希。最终发布不得宣传 reproducible build；如实记录。
- 模型权重、`.pdb`、`models/` 均不得进入包。

## 3. 安装 / Install（已验）

在干净 VM / Medium 桌面，普通用户、不要管理员：

- [x] 解压 `stage-vcomp.zip`，无权重、无 `models/`、无 `.pdb`；
- [x] `install.ps1` 安装到 `%LOCALAPPDATA%\ProjectWatashi`，无 UAC；
- [x] HKCU 卸载项 + 开始菜单快捷方式；
- [x] `watashi_ui.exe --version` 输出 1.0.0-rc1 + commit + Release；
- [x] `watashi_ui.exe --settings` 打开设置页；
- [x] `vcomp140.dll` 随 payload 安装（干净 VM 必需）。

## 4. 运行 / Run（已验）

- [x] synthetic/fake 60 s 运行，悬浮窗/点击穿透正常；
- [x] `--control auto` 在 tray 不可用时自动显示 `watashi control`；
- [x] Pause/Resume、Hide/Show、Exit；
- [x] Exit 后 report：`exit_code=0`、`exit_reason=control-exit`、`subtitles>0`；
- [x] `stream.exit_code=-1` 是主动停止哨兵，不是失败；
- [x] 日志有 start/exit，无字幕原文。

## 5. 卸载 / Uninstall（已验）

- [x] payload/快捷方式/HKCU 项移除；
- [x] `user\` 默认保留（`Test-Path` = True）；
- [ ] `-RemoveUserData` 单独复验（可选，未做）。

## 6. 模型 / Models（未验）

- [ ] `watashi_fetch.exe --check` 缺模型返回 1；
- [ ] `--offline` 拒绝 http；
- [ ] 实际 `--fetch` 下载 + 哈希/原子改名；
- [x] 安装包内无权重；
- [x] 许可文档存在（MIT / CC-BY-NC-4.0 / Apache-2.0）。

## 7. Soak / 长稳（已验）

- [x] 10 分钟：`build-rc/soak-10m.json`，pass=true；
- [x] 8 小时：`build-rc/soak-8h.json`，pass=true，28801.5 s，exit 0。

## 8. 签名 / Signing

- [x] 当前发布为 **unsigned**（无证书）；
- [ ] 有证书时 `sign.ps1` + signtool verify（未验）。

## 9. 已知限制 / Known limitations

1. 本机/VM 无可用通知区，tray 为 best-effort；控制窗/热键是发布必需项。
2. VM 为 BIOS + TPM/SecureBoot 绕过安装；UEFI/安全启动未验收。
3. 未做实际模型下载；`--check`/离线拒绝仍未验。
4. 构建不可逐字节复现；不得宣传 reproducible build。
5. 发布 unsigned；无证书。

## 10. 下一步 / Next

Stage D：升 1.0.0，clean build，生成
`dist/ProjectWatashi-1.0.0-win64.zip` + LF checksums，更新本文件与
`docs/ACCEPTANCE.md`，本地打 `v1.0.0` tag；不 push。
