# v1.0.0 之后 / Post-release plan

状态日期：**2026-11-12**。
当前发布：**v1.0.0 本地 annotated tag**，`main` 领先远端 12 个提交，tag 未 push；
最终包在 `dist/ProjectWatashi-1.0.0-win64.zip`（不进 git）。

## 0. 结论

**先验证和修复，不继续开发新功能。**

v1.0.0 已通过 RC1 的 B 验收（Medium、干净 VM、控制面、安装/卸载、8h soak），
但还有三项“已发布但未证明”的 P0：

1. 发布物没有远端备份/远端 tag；
2. 真实模型（PP-OCRv4 + NLLB）没有在最终包上跑通；
3. 最终 1.0.0 ZIP 没有重新走一次安装验收（验收对象是 RC1 的 `stage-vcomp.zip`）。

P0 完成前，不开 v1.1、不做大规模架构重构。

## 1. P0 · 发布收口（预计 2–4 h）

| # | 任务 | 通过标准 |
| --- | --- | --- |
| P0.1 | push `main` + `v1.0.0` tag | `git ls-remote` 能看到 tag/commit |
| P0.2 | 备份 ZIP + checksum 到第二个盘/外部存储 | 副本 `sha256sum -c` 通过 |
| P0.3 | GitHub Release（可选，但建议） | Release 有 ZIP + SHA256SUMS |
| P0.4 | 确认远端 tag 指向 `a0ecccd` | tag 未移动 |

## 2. P0 · 真实模型端到端（预计 3–6 h）

| # | 任务 | 通过标准 |
| --- | --- | --- |
| P0.5 | `watashi_fetch --check` 缺模型 | 退出码 1，打印缺失项，不联网 |
| P0.6 | `--offline` | 拒绝 http，退出码 1 |
| P0.7 | 实际 `--fetch` | SHA-256/字节数/原子改名通过 |
| P0.8 | 真实 OCR + NMT 运行 | `--ocr onnx` + `--translator nmt/hybrid` 有字幕、有 backend、无崩溃 |
| P0.9 | 报告 | 记录模型加载、首帧/延迟、内存、失败原因 |
| P0.10 | 最终 ZIP 安装后复跑 | 标准用户安装的 `watashi_ui.exe` 上同样通过 |

模型下载约 1.1 GB；网络/体积需用户确认。不能用 fake/corpus 冒充真实模型验收。

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
