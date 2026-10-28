# 交接说明 / Handover

> 给接手的人或编码代理：**先读这一页，再读别的。**
> 最后更新：2026-10-29 07:28（Asia/Shanghai）

## 一句话状态

Python 原型是**可运行、已实测**的参考实现；C++ 主干已是可编译的 walking skeleton
（`watashi_core` / `watashi_cli` / ctest），W1.1 起接入**可选**的 ONNX Runtime OCR，默认仍走 fake。
正式版按 [docs/cpp-v1-plan.md](docs/cpp-v1-plan.md) 执行，当前处于 **W1（内核切片）**：W1.0 骨架、W1.1 可选 ONNX OCR 已完成。

## 必读顺序（约 30 分钟）

1. [docs/cpp-v1-plan.md](docs/cpp-v1-plan.md) — 唯一的执行计划：范围、阶段、人时、验收、风险
2. [README_zh.md](README_zh.md) — 实际能力与已知限制（英文版 [README.md](README.md)）
3. [prototype/README.md](prototype/README.md) — 实测数据、设计权衡、失败模式
4. [prototype/watashi/events.py](prototype/watashi/events.py) — 事件/命令契约，**冻结对象**
5. [THIRD_PARTY_LICENSES.md](THIRD_PARTY_LICENSES.md) — 许可边界（代码 MIT，NLLB 权重 CC-BY-NC-4.0）

## 什么是真的（可复现）

- `prototype\run.cmd --selftest` 通过；21 个自检脚本中 16 个无界面，本轮 15 绿、820 项断言（`selfcheck_web` 需非沙箱环境）
- 实测：OCR 89 / 104 / 117 ms @640 / 960 / 1280 px；整帧 93 ms；94% 的帧被变化检测跳过；NMT int8 215 ms/句、加载 1.4 s
- 模型：NLLB 618 MB、Whisper 464 MB（均为 CTranslate2 int8），PP-OCRv4 16 MB 随 pip 包
- 能力面：M1–M3 + 直播语音 + 桌面窗 + Web 面板 + 声明式呈现 + 插件 v1

## 什么是过期的（别照着做）

| 位置 | 写着 | 实情 |
| --- | --- | --- |
| [Project Watashi.md](Project%20Watashi.md) | “8 个自检脚本、298 项断言” | 0.0.2 时代的数字；现在是 21 个脚本、1119 项（含界面） |
| [docs/screen_ocr_design.md](docs/screen_ocr_design.md) | 建议“降采样后再 OCR” | 实测是陷阱：640 px 会把词粘在一起，导致语料命中失败 |
| [CMakeLists.txt](CMakeLists.txt) | PaddleOCR / llama.cpp 选项 | 验证过的栈是 ONNX Runtime（OCR）+ CTranslate2（NLLB/Whisper） |
| [src/](src/) 与 [include/](include/) | 任何“实现” | 全是占位，且从未编译；不要续写，按计划重开 |

## 硬约束（违反即返工）

1. 事件 JSON 与 [events.py](prototype/watashi/events.py) 逐字段一致（`v`/`seq`/`ts` 信封），不许自创形状。
2. 设置项键名与默认值来自 [settings_schema.py](prototype/watashi/settings_schema.py)，不许加未登记键。
3. 语料命中即权威，模型不得改写术语；未命中落到模型时必须报告**兜底原因**。
4. 推理后端必须实现现有 `Translator` 协议；v1.0 只允许本地后端，外部/局域网后端默认关闭且需先有 ADR。
5. 模型权重不进 git、不进安装包（见 [THIRD_PARTY_LICENSES.md](THIRD_PARTY_LICENSES.md)）。

## 当前状态（2026-10-29）

| 项 | 状态 |
| --- | --- |
| HEAD | `df66a24`（文档：外部后端可选）；tag `v0.0.8a` 在 `e571cd7` |
| 工作区 | 干净：W1.1（`b59d4b9`）与文档提交（`df66a24`）均已推送 |
| 工具链 | VC 工作负载已装；`cl` 需 Developer PowerShell / vcvars |
| CI | `.github/workflows/cpp.yml`：Windows 构建 + ctest + 双校验 |
| 契约 | `schema/` 三份；events payload 已开放，ReadyInfo 必填 3 字段 |
| 黄金样本 | 200 条 + `golden/verify_cases.py` |

## 第一批任务（W0，已完成）

| # | 任务 | 验收 |
| --- | --- | --- |
| W0.1 | 提交工作区并打 `v0.0.8a` | `git status` 干净；tag 指向含直播语音的提交 |
| W0.2 | 装 VS“C++ 桌面开发”；vcpkg；CI 加 Windows 构建 | CI 上第一次 `cl.exe` 编译成功 |
| W0.3 | 三份契约导出 JSON Schema | `schema/*.json` 带版本号，Python 侧校验通过 |
| W0.4 | 黄金样本 200 条 | 同一输入两次运行结果一致 |
| W0.5 | 模型哈希清单 | 每个权重有 SHA-256、来源、许可 |
| W0.6 | 推理栈 spike | ORT 跑通一帧、CT2 跑通一句，记录 P50/P95 |

### W0 交付记录（2026-10-28）

实际提交顺序是：

1. `bff1add` W0.3 三份 JSON Schema
2. `189ae88` W0.4 200 条黄金样本
3. `e002380` W0.5 models.lock.json + `fetch_model --check`
4. `3e664ad` W0.3 补丁：settings 里允许值为 `null` 的字段

`3e664ad` 是在 W0.5 之后才补的，不是 W0.3 原始提交的一部分。检查 W0.3 时应以
`3e664ad` 或之后为准；`189ae88` 上的 settings schema 仍然会把
`det_limit_side_len: null` 和 `profile: null` 判为非法。历史不重写，以此记录为准。

**W0 全部完成前，不要写 W1 的业务代码。** 这不是流程洁癖：契约还在变，先写的会白写。

## 环境坑（本机实测）

- Windows 的 `python` 可能是应用执行别名 → 一律用 `prototype\run.cmd`
- VS Build Tools 2022 已装但**没有 C++ 工作负载**（只有 MSBuild / Common7）
- `selfcheck_web` 在受限沙箱里会 `WinError 5`（CreatePipe），是环境限制，不是代码缺陷
- `cmake` 在 Git Bash 下会段错误，改用 PowerShell
- `models/` 1.1 GB 已被 gitignore；不要提交
- 目录里可能出现 `.dsh-ops-update-*.lock`：代理工具的临时锁，已加入 `.git/info/exclude`，不要提交

## 交接检查单

- [ ] 工作区已提交并打 tag
- [ ] CI 能编译 C++
- [ ] 契约 schema 已导出
- [ ] 黄金样本 ≥200
- [ ] 模型哈希清单
- [ ] 接手方能独立跑通 `prototype\run.cmd --selftest`
- [ ] 接手方能说出“哪些文档是过期的”
