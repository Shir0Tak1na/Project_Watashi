# 发布验收清单 / Release checklist

状态日期：**2026-11-12**（**v1.0.0 已本地发布并 push 到远端**；tag 未移动；包已备份到第二个盘；真实模型与最终 ZIP 安装复验仍待做）。

## 0. 当前状态 / Status

| 项目 | 状态 |
| --- | --- |
| 版本 | **1.0.0**（CMake/manifest/SBOM/--version 统一；RC1 之后未改产品行为） |
| 最终包 | `dist/ProjectWatashi-1.0.0-win64.zip`，16,483,547 字节 |
| SHA-256 | `da6f0c99275d20699569faa421159067dd6a42da73d40d5b27a9997df62bd1c6` |
| checksum | `dist/SHA256SUMS.txt`（UTF-8 无 BOM、LF）；`sha256sum -c` → **OK** |
| clean build | `git archive 7a2c1a0` → `build-final/clean-src` → **318/318 CTest 通过** |
| 10 分钟 UI soak | **PASS**：`build-rc/soak-10m.json`，602.5 s，exit 0，log_errors 0 |
| 8 小时 UI soak | **PASS**：`build-rc/soak-8h.json`，28801.5 s，exit 0，handles 169→169，内存 15.0→13.8 MiB（peak 16.4），log_errors 0 |
| Medium 桌面安装/运行/卸载 | **PASS（用户 2026-11-11）**，对象为 RC1 验收包 |
| 干净 Win11 VM 安装/运行/卸载 | **PASS（用户 2026-11-11）**，对象为 RC1 验收包；VM 为 **BIOS + LabConfig 绕过 TPM/SecureBoot**，不是 UEFI 验收 |
| 控制面 | **PASS**：`control_surface=window`，Pause/Hide/Exit，`exit_reason=control-exit` |
| 托盘 | **不可用**（本机/VM 均无正常通知区）；best-effort，不是 release blocker，**未声称 PASS** |
| 模型下载 | 未执行 `--fetch`；`--check` 未验 |
| 签名 | **unsigned**；无证书 |
| 远端 | **已 push**：`origin/main` = `4ea7a2a`；远端 annotated tag `v1.0.0`（对象 `50f5aab`）deref → `a0ecccd`（见 §10） |
| 备份 | **已备份**：`D:\WatashiRelease\v1.0.0\`（ZIP + SHA256SUMS），副本 `sha256sum -c` → OK |
| GitHub Release | **未创建**（本机无 `gh`/token）；手工步骤见 §10.2 |

> **人工验收与最终包的关系**：RC1 的 B 验收（见 `docs/ACCEPTANCE.md`）是对
> `stage-vcomp.zip` 完成的；最终 1.0.0 包与验收包的 payload 文件列表一致
> （自动 ZIP 审计核对），差异仅为版本字符串与 SBOM 日期。人工结果作为
> 最终 1.0.0 的验收输入沿用，历史结果未改写。
>
> **skipped ≠ passed**：本次 clean build 中 `installer_install` /
> `installer_remove_user_data` 在本机实际执行并通过（staging prefix 安装），
> 并非 skip；在 Low IL 宿主上它们会以 77 skip。

## 1. 最终发布产物 / Final release artefact

- `dist/ProjectWatashi-1.0.0-win64.zip`，**16,483,547 字节**
- SHA-256：`da6f0c99275d20699569faa421159067dd6a42da73d40d5b27a9997df62bd1c6`
- `dist/SHA256SUMS.txt`：UTF-8 无 BOM、LF-only、`<hash>  <filename>` 两空格；
  `sha256sum -c` → **OK**
- ZIP 审计（29 项）：
  - 必含：`vcomp140.dll`、`onnxruntime.dll`、`ctranslate2.dll`、`dnnl.dll`、
    VC runtime（msvcp140*/vcruntime140*/concrt140，8 个）、`licenses/`（4 份
    许可 + SBOM）、`config/`（presentation/settings_ui/install_manifest）、
    `install.ps1`/`uninstall.ps1`/`install_manifest.json`、三个 exe、
    `models.lock.json`
  - 版本：`install_manifest.json` 与 `licenses/sbom.spdx.json` 内均为 1.0.0，
    无 rc1 残留；包内三个 exe `--version` 输出
    `Project Watashi 1.0.0 / commit 7a2c1a0f2eb4 / build Release`
  - 禁含核对：无 `.pdb`、无 `.onnx`、无 `.bin`、无 `models/` 目录（0 项命中）
- 历史包（证据，不再分发）：`build-rc/stage-vcomp.zip`（RC1 B 验收对象）、
  `build-rc/archive/rc1/ProjectWatashi-1.0.0-rc1-win64.zip`

## 2. clean-room / reproducibility

- 最终 clean-room：`git archive 7a2c1a0`（版本 commit）→
  `build-final/clean-src`，独立 configure/build 于 `build-final/clean-build`；
  **318/318 CTest 通过**；`tools/make_sbom.py --check` 通过；
  `vcomp140.dll` 确认随 `cmake --install`/CPack 进入包（ZIP 审计 + installer
  测试 staging 均含）。
- RC1 clean-room（历史）：`git archive` → `build-rc/clean-build`，313/313。
- **不是逐字节可复现**：此前对比有 9 个文本文件 CRLF/LF 不同、4 个二进制
  同尺寸不同哈希。不得宣传 reproducible build；如实记录。
- 模型权重、`.pdb`、`models/` 均不进包（本次已核对为 0 命中）。

## 3. 安装 / Install（RC1 验收已覆盖）

在干净 VM / Medium 桌面，普通用户、不要管理员：

- [x] 解压验收包，无权重、无 `models/`、无 `.pdb`；
- [x] `install.ps1` 安装到 `%LOCALAPPDATA%\ProjectWatashi`，无 UAC；
- [x] HKCU 卸载项 + 开始菜单快捷方式；
- [x] `watashi_ui.exe --version` 输出 1.0.0-rc1 + commit + Release（最终包输出 1.0.0，见 §1）；
- [x] `watashi_ui.exe --settings` 打开设置页；
- [x] `vcomp140.dll` 随 payload 安装（干净 VM 必需）。

## 4. 运行 / Run（RC1 验收已覆盖）

- [x] synthetic/fake 60 s 运行，悬浮窗/点击穿透正常；
- [x] `--control auto` 在 tray 不可用时自动显示 `watashi control`；
- [x] Pause/Resume、Hide/Show、Exit；
- [x] Exit 后 report：`exit_code=0`、`exit_reason=control-exit`、`subtitles>0`；
- [x] `stream.exit_code=-1` 是主动停止哨兵，不是失败；
- [x] 日志有 start/exit，无字幕原文。

## 5. 卸载 / Uninstall（RC1 验收已覆盖）

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

1. 本机/VM 无可用通知区，tray 为 best-effort；控制窗/热键是发布必需项；tray 实点未验。
2. VM 为 BIOS + TPM/SecureBoot 绕过安装；UEFI/安全启动未验收。
3. 未做实际模型下载；`--check`/离线拒绝仍未验。
4. 构建不可逐字节复现；不得宣传 reproducible build。
5. 发布 unsigned；无证书。

## 10. 发布记录 / Release record

### 10.1 本地 tag 与推送（2026-11-12 完成）

- 版本 commit：`7a2c1a0`（chore: bump the product version to 1.0.0）；
  文档收口 commit `a0ecccd`；**annotated tag `v1.0.0` 指向 `a0ecccd`**。
- 最终包由 `7a2c1a0` 的 clean 树构建（见 §2）；`a0ecccd` 仅含文档。

推送结果（`git push origin main` / `git push origin v1.0.0`，均成功，**无 force**）：

| 项 | 值 |
| --- | --- |
| `origin/main` | `4ea7a2a69219deff83899324c94bad43a6f014a8`（`d739086..4ea7a2a`） |
| 远端 tag 对象 | `50f5aabbcadd831c96ab2d411678a57859bd5212`（`refs/tags/v1.0.0`） |
| 远端 tag deref | `a0ecccde1aeb1ee4aa7414cfc7b6401c5e26878b` |
| tag 是否移动 | **否**：本地 deref 与远端 deref 同为 `a0ecccd`，tag 创建后未重建 |

`git ls-remote origin refs/heads/main refs/tags/v1.0.0 'refs/tags/v1.0.0^{}'`
实测输出与上表一致。

### 10.2 备份（2026-11-12 完成）

| 项 | 值 |
| --- | --- |
| 备份路径 | `D:\WatashiRelease\v1.0.0\` |
| 内容 | `ProjectWatashi-1.0.0-win64.zip`（16,483,547 字节）、`SHA256SUMS.txt`（97 字节） |
| 校验 | 在副本目录执行 `sha256sum -c SHA256SUMS.txt` → `ProjectWatashi-1.0.0-win64.zip: OK`（exit 0） |
| 副本 SHA-256 | `da6f0c99275d20699569faa421159067dd6a42da73d40d5b27a9997df62bd1c6`（与 `dist/` 一致） |

备份是本地第二个物理卷（`D:` Data，NTFS），**不是**异地/云备份；两者同时
损坏即丢失，后续可再加一份离线副本。

### 10.3 GitHub Release（未创建）

本机**没有安装 `gh`**，也没有可用的 `GH_TOKEN`/`GITHUB_TOKEN`，因此**没有创建
Release**，也未伪造任何 Release URL。手工上传步骤：

1. 打开 <https://github.com/Shir0Tak1na/Project_Watashi/releases/new>；
2. Tag 选已有的 `v1.0.0`（不要新建 tag）；
3. 标题填 `Project Watashi v1.0.0`；
4. 说明正文粘贴 `docs/RELEASE.md` 的 §0 状态表、§1 产物与 §9 已知限制；
5. 上传资产：
   - `E:\ProjectWatashi\dist\ProjectWatashi-1.0.0-win64.zip`
   - `E:\ProjectWatashi\dist\SHA256SUMS.txt`
6. 发布后核对页面显示的 SHA-256 与 `da6f0c99…bd1c6` 一致。

## 11. 发布后 P0 / Post-release P0

1. ~~push `main` + `v1.0.0` tag，并 `git ls-remote` 验证~~ → **已完成（§10.1）**；
2. ~~把 `dist/ProjectWatashi-1.0.0-win64.zip` + `SHA256SUMS.txt` 备份到第二个盘/外部存储~~ → **已完成（§10.2）**；
3. GitHub Release 上传（§10.3，待办，需要 `gh` 或网页操作）；
4. 真实模型：`watashi_fetch --check` → 实际 `--fetch` → `--ocr onnx` + `--translator nmt/hybrid`；
5. 用最终 1.0.0 ZIP 做一次干净安装/运行/卸载；
6. 只有 P0 暴露问题才发 v1.0.1；P0 通过才进入架构债/v1.1。

详见 [docs/POST-V1.0.md](POST-V1.0.md)。
