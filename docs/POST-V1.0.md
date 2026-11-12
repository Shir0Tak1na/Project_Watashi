# v1.0.0 之后 / Post-release plan

状态日期：**2026-11-12**。
当前发布：**v1.0.0 已 push 到远端**（`origin/main` = `4ea7a2a`，tag `v1.0.0`
deref = `a0ecccd`，未移动）；最终包
`dist/ProjectWatashi-1.0.0-win64.zip`（不进 git）已备份到
`D:\WatashiRelease\v1.0.0\`，副本 `sha256sum -c` 通过。GitHub Release 尚未
创建（本机无 `gh`）。2026-11-12 另有三个发布后缺陷修复（未升版本、未重打包、
未移动 tag），见 §2.6。

## 0. 结论

**先验证和修复，不继续开发新功能。**

v1.0.0 已通过 RC1 的 B 验收（Medium、干净 VM、控制面、安装/卸载、8h soak），
但还有三项“已发布但未证明”的 P0：

1. ~~发布物没有远端备份/远端 tag~~ → **已完成 2026-11-12**（push + 本机第二卷备份；仍无异地副本）；
2. 真实模型（PP-OCRv4 + NLLB）没有在最终包上跑通；
3. 最终 1.0.0 ZIP 没有重新走一次安装验收（验收对象是 RC1 的 `stage-vcomp.zip`）。

P0 完成前，不开 v1.1、不做大规模架构重构。

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
| P0.7 | 实际 `--fetch` | SHA-256/字节数/原子改名通过 | **未验（仅网络路径）**：本机 WinHTTP 为 direct access、系统代理在 WinINet，`watashi_fetch` 报 `send failed`；字符表已用 file:// 镜像跑通下载/校验/原子改名（§2.6） |
| P0.8 | 真实 OCR + NMT 运行 | `--ocr onnx` + `--translator nmt/hybrid` 有字幕、有 backend、无崩溃 | **已修复（2026-11-12）**：真实路径报告 `backend="onnx, local-nmt/int8"`、`trace="ocr: onnx\nlocal-nmt/int8"`，见 §2.3/§2.6 |
| P0.9 | 报告 | 记录模型加载、首帧/延迟、内存、失败原因 | **通过**：真实 backend/trace 已进入事件与 report；NMT 加载 707–816 ms、内存 10.9–11.7 MiB、exit 0 |
| P0.10 | 最终 ZIP 安装后复跑 | 标准用户安装的 `watashi_ui.exe` 上同样通过 | **功能通过**：最终 1.0.0 ZIP 安装后跑通；安装进程为提升令牌，见 §2.1 |

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
在这一宿主上的删除被拦截（沙箱/EPP 层），根因未完全确认。因此“最终 ZIP 在真实
用户目录卸载”记为 **未验**，需在干净 VM 复核；`%LOCALAPPDATA%` 残留需手工清理。

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

**仍未验**：1.1 GB 模型的真实网络下载。本机 Windows 系统代理在 WinINet
（`127.0.0.1:7891`），而 `watashi_fetch` 的 WinHTTP 为 direct access，
因此报 `send failed`；这是宿主网络配置问题，不是本次三处修复的行为回归。

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
