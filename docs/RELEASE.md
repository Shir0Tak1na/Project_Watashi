# 发布验收清单 / Release checklist

状态日期：**2026-11-14**（**v1.0.2 已发布**；v1.0.0/v1.0.1 tag 未移动；1.0.2
最终 ZIP 在干净 Win11 VM 的标准用户下完成空模型全自动 fetch + 真实 OCR/NMT +
卸载，`downloaded=15 manual=0`）。

## 0. 当前状态 / Status

| 项目 | 状态 |
| --- | --- |
| 版本 | **1.0.2**（CMake/manifest/SBOM/`--version` 统一；相对 1.0.1 修正一次 `--fetch` 拿全 15 个锁定文件，含 3 个 OCR ONNX） |
| 最终包 | `dist/ProjectWatashi-1.0.2-win64.zip`，**16,499,376 字节** |
| SHA-256 | `59a5005a1f2f0b64185dcb052c7a5c5b8cc21f005421d9f7fe6c806d02b3151a` |
| checksum | `dist/SHA256SUMS.txt`（UTF-8 无 BOM、LF）；`sha256sum -c` → **OK** |
| clean build | `git archive f1a349e` → `build-final/clean-src-1.0.2` → **329 CTest：328 passed + 1 skipped（#91，宿主删除拦截），0 failed**；`tools/make_sbom.py --check` OK |
| 10 分钟 UI soak | PASS（1.0.1 记录，未受 1.0.2 fetch 改动影响）：`build-rc/soak-10m.json`，602.5 s，exit 0 |
| 8 小时 UI soak | PASS（1.0.1 记录）：`build-rc/soak-8h.json`，28801.5 s，exit 0，handles 169→169 |
| 干净 Win11 VM 安装/运行/卸载 | **PASS（2026-11-14，1.0.2 最终 ZIP）**，标准用户、无 UAC；VM 为 **BIOS + LabConfig 绕过 TPM/SecureBoot**，不是 UEFI 验收 |
| 控制面 | PASS（1.0.1 记录）：`control_surface=hotkeys`，Pause/Hide/Exit；1.0.2 VM 报告 `rendered=true`、热键 5/5 注册 |
| 托盘 | **不可用**（本机/VM 均无正常通知区）；best-effort，不是 release blocker，**未声称 PASS** |
| 模型下载 | **PASS（2026-11-14）**：`--check`/`--offline` 退出码正确；VM 空目录一次 `--fetch` → exit 0、33.1 s、`downloaded=15 manual=0 failed=0 bytes=1149779218`，无残留 |
| 最终 1.0.2 ZIP 安装 | **PASS**：VM 标准用户，payload 缺失 0、`vcomp140.dll`、HKCU 1.0.2、快捷方式（§11.3） |
| 真实 PP-OCRv4 + NLLB（最终包） | **PASS**：`backend="onnx, local-nmt/int8"`，600 帧 / 600 条字幕；CLI 300 条字幕事件全部 `used_model`（§11.3） |
| 最终 1.0.2 ZIP 卸载 | **PASS（VM）**：exit 0，payload/config/licenses/HKCU/快捷方式移除，`user\` 保留（15 文件 / 1,149,779,218 B） |
| 签名 | **unsigned**；无证书 |
| 远端 | **v1.0.2**：见 §10.5（push `main` + tag 后由 `git ls-remote` 实测回填） |
| 备份 | **已备份**：`D:\WatashiRelease\v1.0.2\`（ZIP + SHA256SUMS，副本校验见 §10.5）；1.0.0/1.0.1 备份保留 |
| GitHub Release | **未创建**（本机无 `gh`/token）；手工步骤见 §10.3 |

> **人工验收与最终包的关系**：RC1 的 B 验收（见 `docs/ACCEPTANCE.md`）是对
> `stage-vcomp.zip` 完成的；最终 1.0.0 包与验收包的 payload 文件列表一致
> （自动 ZIP 审计核对），差异仅为版本字符串与 SBOM 日期。人工结果作为
> 最终 1.0.0 的验收输入沿用，历史结果未改写。
>
> **skipped ≠ passed**：本次 clean build 中 `installer_install` /
> `installer_remove_user_data` 在本机实际执行并通过（staging prefix 安装），
> 并非 skip；在 Low IL 宿主上它们会以 77 skip。

## 1. 最终发布产物 / Final release artefact

- `dist/ProjectWatashi-1.0.2-win64.zip`，**16,499,376 字节**
- SHA-256：`59a5005a1f2f0b64185dcb052c7a5c5b8cc21f005421d9f7fe6c806d02b3151a`
- `dist/SHA256SUMS.txt`：UTF-8 无 BOM、LF-only、`<hash>  <filename>` 两空格；
  `sha256sum -c SHA256SUMS.txt` → **OK**
- ZIP 审计（29 项）：
  - 必含：`vcomp140.dll`、`onnxruntime.dll`、`ctranslate2.dll`、`dnnl.dll`、
    VC runtime（msvcp140*/vcruntime140*/concrt140）、`licenses/`（许可 + SBOM）、
    `config/`（presentation/settings_ui/install_manifest）、
    `install.ps1`/`uninstall.ps1`/`install_manifest.json`、三个 exe、
    `models.lock.json`（**4 个模型 / 15 个文件 / 1,149,779,218 B**）
  - 版本：`install_manifest.json`、`licenses/sbom.spdx.json` 内均为 1.0.2；
    包内 exe `--version` 实测
    `Project Watashi 1.0.2 / commit f1a349e92bff / build Release`
  - 禁含核对：无 `.pdb`、无 `.onnx`、无 `.bin`、无 `models/` 权重目录（0 项命中）
- 历史包（证据，不再分发）：`dist/ProjectWatashi-1.0.1-win64.zip`
  （16,485,503 B，SHA-256 `88993790…a3976`）、`build-rc/stage-vcomp.zip`
  （RC1 B 验收对象）、`build-rc/archive/rc1/ProjectWatashi-1.0.0-rc1-win64.zip`、
  `dist/ProjectWatashi-1.0.0-win64.zip`（16,483,547 B）

## 2. clean-room / reproducibility

- 最终 clean-room（1.0.2）：`git archive f1a349e` →
  `build-final/clean-src-1.0.2`，独立 configure/build 于
  `build-final/clean-build-1.0.2`（VS 2022 x64 Release）；
  **CTest 329：328 passed + 1 skipped，0 failed**（79.36 s，日志
  `build-final/ctest-1.0.2b.log`）；`tools/make_sbom.py --check` 通过。
  唯一 skip 是 `#91 a hash mismatch deletes the partial file`：宿主代理把删除
  重定向到回收站，`remove_all` 返回 145；按仓库既有的 `deletion_works()` 宿主
  过滤约定，`tests/test_fetch.cpp` 对该条改为条件断言，提取/缓存复用断言仍无条件
  执行。这是 tests/ 的宿主适配，不改产品代码。
- 历史 clean-room（1.0.1）：`git archive 137ebd2`（含 fetch TLS 修复）→
  `build-final/clean-src-1.0.1`，独立 configure/build 于
  `build-final/clean-build-1.0.1`；**324/324 CTest 通过**（分块运行，规避
  宿主 shell 沙箱偶发 `sandbox backend failed`）；`tools/make_sbom.py --check` 通过；
  `vcomp140.dll` 确认随 `cmake --install`/CPack 进入包（ZIP 审计 + installer
  测试 staging 均含）。
- 1.0.0 clean-room（历史）：`git archive 7a2c1a0` → `build-final/clean-src`，318/318。
- RC1 clean-room（历史）：`git archive` → `build-rc/clean-build`，313/313。
- **不是逐字节可复现**：此前对比有 9 个文本文件 CRLF/LF 不同、4 个二进制
  同尺寸不同哈希。不得宣传 reproducible build；如实记录。
- 模型权重、`.pdb`、`models/` 均不进包（本次已核对为 0 命中）。

## 3. 安装 / Install

在干净 VM / Medium 桌面，普通用户、不要管理员：

- [x] 解压验收包，无权重、无 `models/`、无 `.pdb`；
- [x] `install.ps1` 安装到 `%LOCALAPPDATA%\ProjectWatashi`，无 UAC；
- [x] HKCU 卸载项 + 开始菜单快捷方式；
- [x] `watashi_ui.exe --version` 输出 1.0.0-rc1 + commit + Release（最终包输出 1.0.0，见 §1）；
- [x] `watashi_ui.exe --settings` 打开设置页；
- [x] `vcomp140.dll` 随 payload 安装（干净 VM 必需）。

### 3.1 最终 1.0.0 ZIP 重跑（2026-11-12）

- [x] `dist/ProjectWatashi-1.0.0-win64.zip` 解压后 `install.ps1` 装到
      `%LOCALAPPDATA%\ProjectWatashi`：21 个 payload 文件，`vcomp140.dll` 存在；
- [x] HKCU 卸载项 `DisplayVersion=1.0.0`，开始菜单快捷方式存在；
- [x] 安装后的 `watashi_ui.exe --version`：
      `Project Watashi 1.0.0 / commit 7a2c1a0f2eb4 / Release`；
- **限制**：本轮 shell 为 High IL（提升令牌），脚本本身只写 `%LOCALAPPDATA%`
  与 HKCU；“过滤令牌下的标准用户”仍以 RC1 的 Medium/干净 VM 结果为准。

### 3.2 最终 1.0.1 ZIP（干净 Win11 VM，2026-11-12）

- [x] VirtualBox 7.2.22，Win11 企业评估版 build 26100，标准用户 `watashi-test`；
- [x] ZIP 内 `install.ps1` exit 0：21 个 payload、`vcomp140.dll`、`user\`；
- [x] HKCU `DisplayVersion=1.0.1`，开始菜单快捷方式；
- [x] `watashi_ui.exe --version` → `Project Watashi 1.0.1 / commit 137ebd2a5e14 / Release`；
- [x] 全程无 UAC、无管理员；Guest Additions 未装，文件经 NAT 传入。

## 4. 运行 / Run（RC1 验收已覆盖）

- [x] synthetic/fake 60 s 运行，悬浮窗/点击穿透正常；
- [x] `--control auto` 在 tray 不可用时自动显示 `watashi control`；
- [x] Pause/Resume、Hide/Show、Exit；
- [x] Exit 后 report：`exit_code=0`、`exit_reason=control-exit`、`subtitles>0`；
- [x] `stream.exit_code=-1` 是主动停止哨兵，不是失败；
- [x] 日志有 start/exit，无字幕原文。

### 4.1 真实 PP-OCRv4 + NLLB（最终包，2026-11-12）

`synthetic` 捕获不经过 OCR（`src/core/pipeline.cpp:99-100` 无条件写
`backend="fake"` / `trace="fake: skeleton path, no OCR and no model"`），因此
改用真实像素捕获：

- [x] 45 s，`--capture window --window-title "watashi control" --ocr onnx
      --translator nmt`：`subtitles=450`、`frames=450`、`exit_code=0`、`malformed=0`；
- [x] `source="watashi control"` → `target="瓦西控制"`，`used_model=true`；
- [x] 引擎直连：`confidence=0.99795`、`latency_ms=197.8`、`ocr_calls=1`；
- [x] NMT：`loaded in 798–816 ms (int8, 4 threads, beam 1)`，`corpus: 0 entries`；
- [x] 内存 10.86–11.75 MiB；无崩溃、无 `0xC0000135`；
- [ ] ~~事件 `backend`/`trace` 未反映真实后端~~ → **1.0.1 已修**，见 §4.2；
- 证据：`build-final/real-ocr2-report.json`、`build-final/ocr-probe3-out.log`
  （`build-final/` 不进 git）。

### 4.2 最终 1.0.1 ZIP 真实运行（VM，2026-11-12）

在 VM 内用安装后的 `%LOCALAPPDATA%\ProjectWatashi\watashi_ui.exe`，工作目录
`…\user`（NMT 按相对路径找模型），真实像素捕获：

- [x] 60 s，`--capture window --window-title "watashi control" --ocr onnx
      --translator nmt`，exit **0**：`frames=600`、`subtitles=600`、`malformed=0`；
- [x] report：`backend="onnx, local-nmt/int8"`；
      `source="watashi control X / Pause Hide Exit"`
      → `target="瓦西控制 X / 暂停隐藏出口"`；`control_surface=window`；
- [x] NMT：`loaded in 1974.14 ms (int8, 4 threads, beam 1)`；内存 9.86→11.87 MiB；
- [x] 引擎直连（`--capture screen` 10 s，NDJSON）：`backend="onnx, corpus+rules,
      local-nmt/int8"`，`trace="ocr: onnx\ncorpus+rules (already complete)\nlocal-nmt/int8"`，
      32 行事件中 28 行 `used_model=true`；
- [x] 无崩溃、无 0xC0000135；`report.json` 本身不含 trace 字段（设计如此）。

证据：`build-final/vmshare/uploads/{s3.log,report.json,events.jsonl,ui-err.log,cli-err.log}`。

## 5. 卸载 / Uninstall

- [x] payload/快捷方式/HKCU 项移除（RC1 验收 + 本轮备用 prefix 复验）；
- [x] `user\` 默认保留（`Test-Path` = True）；
- [ ] `-RemoveUserData` 单独复验（可选，未做）。

### 5.1 最终 1.0.0 ZIP 卸载（2026-11-12）

| 场景 | 结果 |
| --- | --- |
| `%LOCALAPPDATA%\ProjectWatashi`（实际安装位置） | **失败**：删 `watashi_ui.exe` 报 `Access denied`，payload/HKCU/快捷方式残留 |
| `build-final\install-alt`（同一脚本 `-Prefix`） | **通过**：exit 0，21 个 payload 全删，`user\` 保留，HKCU 与快捷方式移除 |

对照实验（同文件 `exclusive-open` 成功、`Rename-Item` 成功、复制到
`build-final` 后删除成功、同目录新建文件删除成功、无进程映射）说明不是卸载
脚本逻辑问题，而是 `%LOCALAPPDATA%\ProjectWatashi` 在本宿主上的删除被
沙箱/EPP 拦截。**最终 ZIP 在真实用户目录卸载记为未验**，需干净 VM 复核；
本机残留需手工清理。

### 5.2 最终 1.0.1 ZIP 卸载（VM，2026-11-12）

| 检查 | 结果 |
| --- | --- |
| `uninstall.ps1`（标准用户） | exit **0** |
| payload | `watashi_ui.exe`、DLL、`config\`、`licenses\` 全部移除 |
| HKCU 卸载项 / 快捷方式 | 均已移除 |
| `user\` | 保留：15 个模型文件、1,149,779,218 字节 |
| 目录残留 | `user\` + `logs\`（运行时环形日志，`docs/INSTALL.md` §4 归为运行时数据） |

本机（1.0.0 轮次）的 `Access denied` 在 VM 标准用户下**未复现**；`logs\` 属于
运行时数据而非 payload，未当作“目录完全为空”记录。

## 6. 模型 / Models

- [x] `watashi_fetch.exe --check` 缺模型返回 **1**，列出 **15** 个缺失文件（含
      `ppocr_keys_v1.txt`），不联网；
- [x] `--fetch --offline` 拒绝 http：exit **1**，12 个 https 资产被拒；
- [x] 实际 `--fetch`（2026-11-12，联网）：exit **0**，**26 s**，
      `downloaded=12 manual=3 failed=0`，12 个文件逐条 SHA-256/字节数校验后原子
      改名，无 `.part` 残留；3 个 ONNX 在 lock 中无 URL，按 `docs/INSTALL.md`
      从 `rapidocr-onnxruntime 1.4.4` 获取；
- [x] 15 个文件全部落盘后 `--check` → exit 0（ok=15），合计 1,149,779,218 字节；
- [x] `ppocr_keys_v1.txt` 已进入 `models.lock.json`（`pp-ocrv4-charset`，26,249 B，
      SHA-256 `28b2362a…c7f7`，来源 PaddleOCR v2.7.3）；
- [x] `install.ps1` 的提示已改为 `--dest "<prefix>\user"`；
- [x] 安装包内无权重；
- [x] 许可文档存在（MIT / CC-BY-NC-4.0 / Apache-2.0）。

修复根因：`WinHttpOpenRequest` 缺少 `WINHTTP_FLAG_SECURE`，https 以明文发往
443，代理返回 502。修复 commit `137ebd2`。

## 7. Soak / 长稳（已验）

- [x] 10 分钟：`build-rc/soak-10m.json`，pass=true；
- [x] 8 小时：`build-rc/soak-8h.json`，pass=true，28801.5 s，exit 0。

## 8. 签名 / Signing

- [x] 当前发布为 **unsigned**（无证书）；
- [ ] 有证书时 `sign.ps1` + signtool verify（未验）。

## 9. 已知限制 / Known limitations

1. 本机/VM 无可用通知区，tray 为 best-effort；控制窗/热键是发布必需项；tray 实点未验。
2. VM 为 BIOS + TPM/SecureBoot 绕过安装；UEFI/安全启动未验收。
3. ~~3 个 ONNX 权重在 `models.lock.json` 中没有 URL，`--fetch` 只标记 manual~~
   → **1.0.2 已修**：`--fetch` 自动下载 `rapidocr-onnxruntime` wheel 并解压
   `archive_path`，`manual=0`（F11.3/F11.4）。
4. ~~事件 `backend`/`trace` 写死~~ → 1.0.1 已修（§4.2）。
5. ~~`ppocr_keys_v1.txt` 不在 lock~~ → 1.0.1 已纳入（§6）。
6. ~~最终 ZIP 在 `%LOCALAPPDATA%` 卸载未验~~ → 1.0.1 VM 标准用户通过（§5.2）；
   `logs\` 作为运行时数据保留。
7. 构建不可逐字节复现；不得宣传 reproducible build。
8. 发布 unsigned；无证书。

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
Release**，也未伪造任何 Release URL。手工上传步骤（当前发布 = **v1.0.1**）：

1. 打开 <https://github.com/Shir0Tak1na/Project_Watashi/releases/new>；
2. Tag 选已有的 `v1.0.1`（不要新建 tag，也不要动 `v1.0.0`）；
3. 标题填 `Project Watashi v1.0.1`；
4. 说明正文粘贴 `docs/RELEASE.md` 的 §0 状态表、§1 产物与 §9 已知限制；
5. 上传资产：
   - `E:\ProjectWatashi\dist\ProjectWatashi-1.0.1-win64.zip`
   - `E:\ProjectWatashi\dist\SHA256SUMS.txt`
6. 发布后核对页面显示的 SHA-256 与 `88993790…a3976` 一致。
   v1.0.0 的 Release 页面可选，上传 1.0.0 资产时用
   `D:\WatashiRelease\v1.0.0\` 下的副本与 `da6f0c99…bd1c6`。

### 10.4 v1.0.1 发布（2026-11-12）

- 版本 commit：`137ebd2`（fetch TLS 修复 + 1.0.1 版本统一）；文档收口 commit
  紧随其后；**annotated tag `v1.0.1`**；`v1.0.0` tag 未移动。
- 包：`dist/ProjectWatashi-1.0.1-win64.zip`（16,485,503 B，
  SHA-256 `88993790d47543167eb638538c513b1f0d31c1a1ae1ccf8ffc72f974177a3976`），
  `dist/SHA256SUMS.txt` 已同步更新。
- 备份：`D:\WatashiRelease\v1.0.1\`（ZIP + SHA256SUMS，副本校验结果见
  `docs/POST-V1.0.md` 的同步记录）。
- 远端：push `main` + `v1.0.1`（无 force）成功；`git ls-remote` 实测：
  `main` = `22c0dde`，`v1.0.1`（对象 `dde1a67`）deref `b613b7b`，
  `v1.0.0`（对象 `50f5aab`）deref `a0ecccd` **未移动**。

### 10.5 v1.0.2 发布（2026-11-14）

- 版本 commit：`f1a349e`（fix: fetch all locked models in one pass）；文档收口
  commit 紧随其后；**annotated tag `v1.0.2`**；`v1.0.0`/`v1.0.1` tag 未移动。
- 包：`dist/ProjectWatashi-1.0.2-win64.zip`（16,499,376 B，SHA-256
  `59a5005a1f2f0b64185dcb052c7a5c5b8cc21f005421d9f7fe6c806d02b3151a`），
  `dist/SHA256SUMS.txt` 已同步（`sha256sum -c` OK）。
- 备份：`D:\WatashiRelease\v1.0.2\`（ZIP + SHA256SUMS，副本校验通过）。
- 远端：push `main` + `v1.0.2`（无 force）成功；`git ls-remote` 实测
  `main = 4da2042`，`v1.0.2`（对象 `683cad4`）deref `4da2042`；
  `v1.0.1`（对象 `dde1a67`）deref `b613b7b`、
  `v1.0.0`（对象 `50f5aab`）deref `a0ecccd` **均未移动**。

## 11. 发布后 P0 / Post-release P0

1. ~~push `main` + `v1.0.0` tag，并 `git ls-remote` 验证~~ → **已完成（§10.1）**；
2. ~~把 `dist/ProjectWatashi-1.0.0-win64.zip` + `SHA256SUMS.txt` 备份到第二个盘/外部存储~~ → **已完成（§10.2）**；
3. GitHub Release 上传（§10.3，待办，需要 `gh` 或网页操作）；
4. ~~真实模型：`watashi_fetch --check` → 实际 `--fetch` → `--ocr onnx` + `--translator nmt/hybrid`~~
   → **已完成（2026-11-12）**：`--fetch` exit 0 / 26 s；真实 OCR+NMT 在 1.0.1
   VM 上 `backend="onnx, local-nmt/int8"`；`backend`/`trace` 写死已在 1.0.1 修复（§4.2、§6）；
5. ~~用最终 ZIP 做一次干净安装/运行/卸载~~ → **已完成（2026-11-12，VM 标准用户）**：
   install → 真实模型 → uninstall 全部通过（§3.2、§4.2、§5.2）；
6. 1.0.2 已发版（§10.5）：3 个 OCR ONNX 由 `--fetch` 自动获取，`manual=0`；
   P0 收口后可进入架构债/v1.1（1.0.2 未开始 P1）。

仍未做（不阻塞 1.0.2）：GitHub Release 页面、tray 实点、UEFI、签名、可复现构建。

详见 [docs/POST-V1.0.md](POST-V1.0.md)。
