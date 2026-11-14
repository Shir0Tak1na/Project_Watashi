# v1.0.0 之后 / Post-release plan

状态日期：**2026-11-14**。
当前发布：**v1.0.2 已发布**（v1.0.0/v1.0.1 的远端 tag 未移动）。1.0.2 的包与
校验和见 `dist/`；干净 Win11 VM 全流程见 `docs/ACCEPTANCE.md` §11。GitHub
Release 尚未创建（本机无 `gh`）。
1.0.2 只修一处发布缺陷：`--fetch` 一次拿全 15 个锁定文件（3 个 OCR ONNX 由
wheel 自动解压），`manual=0`；产品功能未变。

## 0. 结论

**P0 收口完成；可以进入 v1.1。** 三项“已发布但未证明”的 P0 现状：

1. ~~发布物没有远端备份/远端 tag~~ → **已完成 2026-11-12**（push + 本机第二卷备份；仍无异地副本）；
2. 真实模型（PP-OCRv4 + NLLB）没有在最终包上跑通 → **完成**：1.0.1 最终 ZIP
   在干净 Win11 VM 的标准用户下跑通（§2.7）；
3. 最终 ZIP 没有重新走安装验收 → **完成**：同一轮 VM 安装/运行/卸载。

仍未做（不阻塞 1.0.2）：tray 真实点击、UEFI、代码签名、构建可复现性、
GitHub Release 页面。

### 0.1 v1.0.2 同步（2026-11-14）

| 项 | 结果 |
| --- | --- |
| clean build | `git archive f1a349e` → CTest 329（328 passed + 1 skipped，宿主删除拦截），SBOM `--check` OK |
| 全自动 fetch | 宿主与 VM 均 `downloaded=15 manual=0 failed=0 bytes=1149779218`；VM 一次成功、33.1 s、无残留 |
| VM 全流程 | 标准用户 install → 空模型 fetch → 真实 OCR/NMT（`onnx, local-nmt/int8`，600 帧/600 字幕；CLI 300 条真实 NMT 事件）→ uninstall（`user\` 保留） |
| 包 | `dist/ProjectWatashi-1.0.2-win64.zip`，16,499,376 B，SHA-256 `59a5005a…b3151a` |
| 远端 | push `main` + `v1.0.2` 完成；`git ls-remote`：`main = 4da2042`，`v1.0.2` deref `4da2042`，`v1.0.0`/`v1.0.1` tag 未移动（RELEASE §10.5） |
| 备份 | `D:\WatashiRelease\v1.0.2\`（ZIP + SHA256SUMS，副本校验通过） |
| 下一步 | 仍是不新增功能的 P1 架构债（UI→core、版本单一来源、CMake/打包拆分）；1.0.2 未开始 P1 |

## 1. P0 · 发布收口

| # | 任务 | 通过标准 | 状态 |
| --- | --- | --- | --- |
| P0.1 | push `main` + `v1.0.0` tag | `git ls-remote` 能看到 tag/commit | **完成**：`4ea7a2a` / tag deref `a0ecccd` |
| P0.2 | 备份 ZIP + checksum 到第二个盘/外部存储 | 副本 `sha256sum -c` 通过 | **完成**：`D:\WatashiRelease\v1.0.0\`，`-c` OK |
| P0.3 | GitHub Release（可选，但建议） | Release 有 ZIP + SHA256SUMS | **未做**：无 `gh`/token；手工步骤见 RELEASE §10.3 |
| P0.4 | 确认远端 tag 指向 `a0ecccd` | tag 未移动 | **完成**：远端 `50f5aab` → `a0ecccd` |

## 2. P0 · 真实模型端到端

| # | 任务 | 通过标准 | 状态（2026-11-12 实测） |
| --- | --- | --- | --- |
| P0.5 | `watashi_fetch --check` 缺模型 | 退出码 1，打印缺失项，不联网 | **通过**：exit 1，列出 15 个缺失文件（含字符表） |
| P0.6 | `--offline` | 拒绝 http，退出码 1 | **通过**：exit 1，12 个 https 资产被拒；3 个 ONNX 文件标记 manual |
| P0.7 | 实际 `--fetch` | SHA-256/字节数/原子改名通过 | **通过（2026-11-12）**：修 TLS 标志后联网 exit 0，26 s，`downloaded=12 manual=3 failed=0`，12 个文件逐条 verified；3 个 ONNX 无 URL，从 PyPI 包复制并核对哈希 |
| P0.8 | 真实 OCR + NMT 运行 | `--ocr onnx` + `--translator nmt/hybrid` 有字幕、有 backend、无崩溃 | **通过（VM 复核）**：`backend="onnx, local-nmt/int8"`，60 s 内 600 条字幕，见 §2.7 |
| P0.9 | 报告 | 记录模型加载、首帧/延迟、内存、失败原因 | **通过**：VM 内 NMT 加载 1974.14 ms、内存 9.86–11.87 MiB、exit 0；trace 取自 NDJSON |
| P0.10 | 最终 ZIP 安装后复跑 | 标准用户安装的 `watashi_ui.exe` 上同样通过 | **通过（2026-11-12）**：干净 Win11 VM 标准用户 `watashi-test`，install → 真实模型 → uninstall，无 UAC，见 §2.7 |

模型下载约 1.1 GB（lock 合计 1,149,779,218 字节）；网络/体积需用户确认。不能用
fake/corpus 冒充真实模型验收。

### 2.1 最终 ZIP 安装（2026-11-12）

- 解压 `dist/ProjectWatashi-1.0.0-win64.zip` 到 `build-final/stage-1.0.0`，
  `install.ps1` 安装到 `%LOCALAPPDATA%\ProjectWatashi`：21 个 payload 文件、
  `vcomp140.dll` 存在、HKCU 卸载项 `DisplayVersion=1.0.0`、开始菜单快捷方式、
  `--version` 输出 `Project Watashi 1.0.0 / commit 7a2c1a0f2eb4 / Release`。
- **限制**：本会话 shell 为 High IL（已提升），脚本本身只写 `%LOCALAPPDATA%`
  与 HKCU、不要求提权，但“过滤令牌下的标准用户”未在本机独立复现；该项仍以
  RC1 的 Medium 桌面 + 干净 VM 结果为准。

### 2.2 模型来源（未联网）

`--fetch` 需要 1.1 GB 网络下载，本轮**未执行**。核对 lock 后发现本机已有等价
文件，逐文件按 `models.lock.json` 校验 SHA-256 与字节数后铺入
`%LOCALAPPDATA%\ProjectWatashi\user\models`：

| 模型 | 文件 | 结果 |
| --- | --- | --- |
| NLLB-200-600M CT2 int8 | 7 个（622 MiB） | 哈希/字节数全部匹配 |
| Whisper small CT2 int8 | 4 个（483 MiB） | 哈希/字节数全部匹配 |
| PP-OCRv4 det/rec/cls ONNX | 3 个（15 MiB） | 哈希/字节数全部匹配 |

铺入后 `watashi_fetch --check --dest <user>` → **exit 0（ok=14）**（lock 变更前
的 14 个文件；加入字符表后应校验 15 个，见 §2.6 的注意项）。

**已修复（2026-11-12）**：`ppocr_keys_v1.txt` 已加入 `models.lock.json`
（`pp-ocrv4-charset`，26,249 字节，SHA-256 `28b2362a…`，来源 PaddleOCR
`v2.7.3` 的 `ppocr/utils/ppocr_keys_v1.txt`），`--check` 现在校验 15 个文件，
`--fetch` 对该文件走显式 URL、校验后原子改名。

**已修复（2026-11-12）**：`install.ps1` 的提示改为
`--dest "<prefix>\user"`，与 lock 的 `models/` 前缀和默认 dest 一致；
`docs/INSTALL.md` 同步说明字符表可自动下载。

### 2.3 真实 OCR + NMT 结果

`synthetic` 捕获**不经过 OCR**：即使传 `--ocr onnx`，事件仍是
`backend:"fake"`、`trace:"fake: skeleton path, no OCR and no model"`
（`src/core/pipeline.cpp` 的固定值），字幕来源为空。因此任务给出的
`--capture synthetic --ocr onnx` 不能用于证明真实模型，已改用真实像素捕获。

用最终 ZIP 安装的 `watashi_ui.exe`（45 s，`--capture window
--window-title "watashi control" --ocr onnx --translator nmt`，工作目录
`<user>`，绝对路径传 det/rec/cls/chars）：

- `subtitles=450`、`frames=450`、`stream.exit_code=0`、`malformed=0`；
- `source="watashi control"`、`target="瓦西控制"`、`used_model=true`；
- 引擎直连复测：`confidence=0.99795`、`latency_ms=197.8`、`ocr_calls=1`；
- NMT：`nmt: loaded in 798–816 ms (int8, 4 threads, beam 1)`，`corpus: 0 entries`；
- 内存 `10.86–11.75 MiB`；无崩溃、无 `0xC0000135`。

**修复前**（2026-11-12 上午）：`src/core/pipeline.cpp:99-100` 无条件写
`backend="fake"` 和 `trace="fake: skeleton path, no OCR and no model"`，
即使真实 OCR/NMT 生效。修复后见 §2.6。

### 2.4 卸载

| 场景 | 结果 |
| --- | --- |
| `%LOCALAPPDATA%\ProjectWatashi`（最终 ZIP 实际安装位置） | **失败**：`Remove-Item` 删 `watashi_ui.exe` 报 `Access denied`，payload/HKCU/快捷方式均残留 |
| `build-final\install-alt`（同一脚本，`-Prefix` 指定） | **通过**：exit 0，21 个 payload 全删，`user\` 保留，HKCU 与快捷方式移除 |

对照实验（均在同一会话）：同文件 `exclusive-open` 成功（无句柄）、`Rename-Item`
成功、复制到 `build-final` 后删除成功、在安装目录新建临时文件后删除成功；无进程
映射该 exe。结论：**不是卸载脚本逻辑问题**，是 `%LOCALAPPDATA%\ProjectWatashi`
在这一宿主上的删除被拦截（沙箱/EPP 层），根因未完全确认。**2026-11-12 在干净
Win11 VM 的标准用户下复核：卸载 exit 0，payload/HKCU/快捷方式全部移除，
`user\` 保留，该残留未复现**（§2.7）。

### 2.5 托盘观察

本轮 120 s 运行首次出现 `tray: icon added`，报告 `tray.added=true`、
`control_surface=tray`；但**没有做真实图标点击**，托盘仍不记为 PASS。

### 2.6 三个 P0 缺陷的修复（2026-11-12）

未升版本、未重打包、未移动 `v1.0.0` tag，也未 push。

1. **backend / trace 写死**（`src/core/pipeline.cpp`）：OCR 层按实现类型命名
   （`FakeOcr` → `fake`，其余为 ONNX 路径 → `onnx`），翻译层取每行
   `Translation.backend`/`trace` 的并集；纯 fake 骨架路径仍输出
   `backend="fake"`、`trace="fake: skeleton path, no OCR and no model"`。
   - 单测：`tests/test_session.cpp` 新增 3 例（fake 语义、真实路径非 fake、
     空帧仍命名 OCR 层）；`tests/test_fetch.cpp` 新增 4 例。
   - 真实模型复跑（重建的 `watashi_cli.exe`，本机已铺模型，未联网）：
     `--capture window --window-title "watashi control" --ocr onnx
     --translator nmt` 5 s → `backend="onnx, local-nmt/int8"`、
     `trace="ocr: onnx\nlocal-nmt/int8"`、`source="watashi control"` →
     `target="瓦西控制"`、`used_model=true`、`confidence=0.9888`、exit 0。
     证据：`build-rc/real-path-events.ndjson`（不进 git）。
2. **`ppocr_keys_v1.txt`**（`models.lock.json`）：新增 `pp-ocrv4-charset`，
   26,249 字节，SHA-256 `28b2362ad4ab2dc38769aa72feb535e3a9ddb3fd2a7585a05920e6393b1dc7f7`，
   来源 PaddleOCR `v2.7.3` `ppocr/utils/ppocr_keys_v1.txt`（已下载比对：
   与从 rec 元数据导出的 6,623 行内容逐字节一致，仅本地导出多一个行尾换行）。
   `--check` 空目录 → exit 1，15 个缺失项含该文件；`--offline` → exit 1，
   12 个 https 资产被拒。file:// 镜像跑通 `--fetch`：下载 1、校验通过、
   26,249 字节、无残留 `.part`，随后 `--check` exit 0。
3. **`install.ps1 --dest` 提示**：改为 `--dest "<prefix>\user"`；
   `docs/INSTALL.md` 同步说明字符表走显式 URL。

构建/回归：`cmake --build build-rc --config Release` 通过；
`ctest` **324/324**（原 318 + 新增 6）。

**注意**：先前铺入 `%LOCALAPPDATA%\ProjectWatashi\user\models` 的字符表是本地
导出的 26,250 字节版本，与新 lock 的 canonical 26,249 字节不一致；用新版
`--check` 会把它判为哈希不符，需重新 `--fetch`（或替换为 canonical 文件）。

**已解决（2026-11-12）**：真实 `--fetch` 的失败不是代理配置，而是
`tools/fetch_core.cpp` 没给 `WinHttpOpenRequest` 传 `WINHTTP_FLAG_SECURE`——
https 被当明文发到 443，代理返回 502。修复后同一请求 200，实际下载
`downloaded=12 manual=3 failed=0`、26 s（§2.7）。

### 2.7 v1.0.1 发布与 VM 验收（2026-11-12）

版本统一为 **1.0.1**（CMake / `WATASHI_VERSION` / `config/install_manifest.json` /
`tools/license_manifest.json` / `licenses/sbom.spdx.json` / `--version`），
产品行为与 1.0.0 相同，只含 §2.6 与 fetch TLS 修复。

| 步骤 | 结果 |
| --- | --- |
| clean build | `git archive 137ebd2` → `build-final/clean-src-1.0.1`，Release 构建通过 |
| 回归 | `ctest` **324/324**（分块运行）；`make_sbom.py --check` OK |
| 打包 | `dist/ProjectWatashi-1.0.1-win64.zip`，16,485,503 B，SHA-256 `88993790…a3976`；`SHA256SUMS.txt` UTF-8 无 BOM / LF，逐文件校验通过 |
| 包内容 | 29 个条目；含 `vcomp140.dll`、`models.lock.json`；无权重、无 `.pdb` |
| VM 安装 | Win11 企业评估版（VirtualBox，headless）标准用户 `watashi-test`：exit 0，21 个 payload，`DisplayVersion=1.0.1`，快捷方式，无 UAC |
| 真实运行 | `--capture window --window-title "watashi control" --ocr onnx --translator nmt` 60 s：600 帧 / 600 条字幕，`backend="onnx, local-nmt/int8"`，`瓦西控制 / 暂停隐藏出口`，NMT 加载 1974 ms，退出码 0 |
| trace | CLI NDJSON：`backend="onnx, corpus+rules, local-nmt/int8"`，`trace="ocr: onnx\ncorpus+rules (already complete)\nlocal-nmt/int8"` |
| VM 卸载 | exit 0；payload/`config\`/`licenses\`/HKCU/快捷方式移除；`user\` 保留（1,149,779,218 B） |

细节与证据见 [docs/ACCEPTANCE.md](ACCEPTANCE.md) §10、[docs/RELEASE.md](RELEASE.md) §10。
VM 内 `logs\` 目录仍然保留（运行时环形日志，属于运行时数据而非 payload），
已如实记录，不当作“目录完全为空”。

远端同步记录（`git ls-remote` 实测，2026-11-12，无 force）：

| ref | 对象 |
| --- | --- |
| `refs/heads/main` | `b613b7b`（`4ea7a2a..b613b7b`） |
| `refs/tags/v1.0.1` | `dde1a67` → deref `b613b7b` |
| `refs/tags/v1.0.0` | `50f5aab` → deref `a0ecccd`（**未移动**） |

## 3. P1 · v1.0.1（仅修 P0 暴露的问题）

触发条件：

- 真实模型路径崩溃/无输出；
- 最终 ZIP 安装与 RC1 验收包行为不一致；
- 安装器缺少运行库（例如 vcomp 类问题再次出现）。

范围限制：

- 只修 release blocker 和回归；
- 不改事件 schema、模型、UI 行为；
- 修复后重新出 1.0.1 包、更新 docs/RELEASE；
- 不因为“顺手”加功能。

**状态（2026-11-12）：已交付。** 触发项是真实 fetch 的 TLS 缺陷（§2.7）；
包、校验和、VM 验收都已完成，tag `v1.0.1`，v1.0.0 tag 未移动。三项架构债
（下面 §4）不在 1.0.1 范围内。

## 4. P1 · 架构债（v1.0.1 之后，v1.1 之前）

这些不阻塞 v1.0 使用，但应在 v1.1 前处理：

1. UI → `watashi_core` 链接：拆 `watashi_contract` / `watashi_win32`，
   让 UI 不链接引擎（当前 `docs/architecture.md` 已如实记录为 debt）；
2. 版本单一来源：`version.txt` / `cmake/Version.cmake`，manifest/SBOM/docs 一致性测试；
3. CMake 依赖/打包拆分与单一打包路径；
4. `watashi_nmt_probe` 默认可选；
5. tests 拆分（可选）。

预计 15–25 h；不赶在 v1.0.0 发布窗口做。

## 5. P2 · v1.1

只有 P0 全绿后才开始：

- 直播语音（WASAPI / Whisper）；
- 插件沙箱；
- 其他非 Windows 平台。

v1.1 不改变 v1.0 的本地优先/无遥测边界。

## 6. 决策规则

- P0 失败 → 修 v1.0.1，不进入 P1/P2；
- P0 通过 → 冻结 v1.0，再排架构债；
- 任何“新功能”在 P0 完成前不合并；
- 不把 tray/UEFI/签名/复现性这些已知限制当成新功能来做，除非用户明确改变验收标准。
