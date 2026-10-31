# 交接说明 / Handover

> 给接手的人或编码代理：**先读这一页，再读别的。**
> 最后更新：2026-10-31 14:40（Asia/Shanghai）

## 一句话状态

C++ 正式版已经从 walking skeleton 走到 **W3.3**：本地捕获、PP-OCRv4、语料/规则、CTranslate2 NLLB、原生 Win32 悬浮窗、声明式呈现、stdin 命令通道、全局热键和托盘都已落地。**W3.3 的托盘独立 owner 与报告退出路径已修**；W3.4 设置页、W4 发布硬化尚未开始。

## 必读顺序（约 30 分钟）

1. [docs/cpp-v1-plan.md](docs/cpp-v1-plan.md) — 执行计划；先看 §0 的当前进度，再看原始 WBS
2. [README_zh.md](README_zh.md) — 实际能力、构建方式与已知限制（英文版 [README.md](README.md)）
3. [prototype/README.md](prototype/README.md) — Python 参考实现的实测数据与失败模式
4. [prototype/watashi/events.py](prototype/watashi/events.py) — 事件/命令契约，**冻结对象**
5. [THIRD_PARTY_LICENSES.md](THIRD_PARTY_LICENSES.md) — 许可边界（代码 MIT，NLLB 权重 CC-BY-NC-4.0）

## 什么是真的（可复现）

- **C++ 测试**：当前 630af73 的 build-w33 通过 **240 用例 / 1,402,011 断言**。W3.1 的提交 1e8aa1a 单独跑有 3 个红测，原因是测试把临时 spec 写进 %TEMP%，本机 ACL 拒绝；W3.2 把测试临时目录改到 build 树后全绿。
- **NMT 差分**：tools/compare_nmt.py **120/120** 与 Python 参考逐字段一致（target/backend/protected_terms/lost_placeholders/fallback_reason/used_model）。
- **NMT 性能**：watashi_nmt_probe 加载 **660.6 ms**；4 句 batch 共 **490.9 ms**（约 123 ms/句），在 350 ms/句的预算内。
- **OCR**：50 张合成帧 + 10 张受控字幕，逐帧均值/中位数/最小 **1.0000**，框数 0 不一致，最大框差 **2 px**；1200×200 P50 **49–52 ms**。
- **捕获**：窗口捕获 P50 **2.05 ms**；屏幕捕获 P50 **264.6 ms**，是 GameViewer 虚拟显示的环境限制（独立 mss 同样 264–268 ms，DXGI E_ACCESSDENIED）。
- **原生 UI**：watashi_ui 点击穿透、置顶、不抢焦点；144 DPI 下 20 帧、click_through=pass、焦点前后不变、内存 **8.4 MiB**；目标文字换行完整。
- **流校验**：fake / corpus / 捕获流仍通过 tools/validate_stream.py 与 events.schema.json（42 事件 + 负例）。

## 什么是过期的（别照着做）

| 位置 | 写着 | 实情 |
| --- | --- | --- |
| [docs/cpp-v1-plan.md](docs/cpp-v1-plan.md) | W1/W3 的旧工作包编号、20 h/周 | 实际交付顺序与编号不同；以 §0 进度和实际提交为准 |
| [Project Watashi.md](Project%20Watashi.md) | “8 个自检脚本、298 项断言” | 0.0.2 时代数字；原型现在是 21 个脚本、1119 项（含界面） |
| [docs/screen_ocr_design.md](docs/screen_ocr_design.md) | 建议“降采样后再 OCR” | 实测是陷阱：640 px 会把词粘在一起，导致语料命中失败 |
| [CMakeLists.txt](CMakeLists.txt) | PaddleOCR / llama.cpp 选项 | 验证过的栈是 ONNX Runtime（OCR）+ CTranslate2（NLLB/Whisper） |
| config/app.yaml | C++ 主配置 | 目前 C++ 不读它；CLI 用显式参数，设置页尚未实现 |

## 硬约束（违反即返工）

1. 事件 JSON 与 [events.py](prototype/watashi/events.py) 逐字段一致（v/seq/ts 信封），不许自创形状。
2. 设置项键名与默认值来自 [settings_schema.py](prototype/watashi/settings_schema.py)，不许加未登记键；W3.4 设置页必须从 schema 生成。
3. 语料命中即权威，模型不得改写术语；未命中落到模型时必须报告**兜底原因**。
4. 推理后端必须实现现有 Translator 协议；v1.0 只允许本地后端，外部/局域网后端默认关闭且需先有 ADR。
5. 模型权重不进 git、不进安装包（见 [THIRD_PARTY_LICENSES.md](THIRD_PARTY_LICENSES.md)）。
6. UI 不得复制引擎逻辑：watashi_ui 只消费 watashi_cli --json-lines，命令只走 stdin 的 JSON 命令通道。

## 当前状态（2026-10-31）

| 项 | 状态 |
| --- | --- |
| HEAD | 630af73（W3.3：独立 tray owner + 正常退出报告） |
| 远端 | d261fad（本地领先 3 个提交；本次文档更新尚未提交） |
| 工作区 | 代码 clean；本文档更新中；build-* 已 gitignore |
| 当前构建 | build-w33（W3.3）；build-w32 保留 W3.2；build-w2 用于 NMT |
| 契约 | schema/ 三份；events payload 开放，ReadyInfo 必填 3 字段 |
| 黄金样本 | 200 条 + golden/verify_cases.py；NMT 另有 golden/nmt 120 条 |
| 当前阻塞 | 交互桌面的托盘菜单尚未手工复验（本 shell 无通知区，tray.added=false） |
| 下一步 | 交互桌面复验托盘/报告；然后 W3.4 设置页 |

## W0 交付记录（2026-10-28）

1. bff1add W0.3 三份 JSON Schema
2. 189ae88 W0.4 200 条黄金样本
3. e002380 W0.5 models.lock.json + fetch_model --check
4. 3e664ad W0.3 补丁：settings 里允许值为 null 的字段

3e664ad 在 W0.5 之后补，检查 W0.3 以它或之后为准；历史不重写。

## W1 交付记录（2026-10-28 → 10-30）

| 提交 | 内容 |
| --- | --- |
| f7f2138 → 3af4f23 | W1.0 骨架、ready/stats、schema 门 |
| b59d4b9 | W1.1 可选 ONNX Runtime OCR；默认 fake |
| 83d2523 | W1.2 语料/规则引擎；120/120 corpus-only 差分；86 用例 |
| 50a33b3 | W1.3 Win32 捕获（screen/window/synthetic）；107 用例 |
| 7c88dfe | W1.4 Python 悬浮窗消费 C++ JSONL；16 适配器用例 |

W1.1 验收：50 合成 + 10 受控字幕一致率 1.0000；1200×200 P50 49–52 ms；框差 ≤2 px。屏幕捕获在本机被虚拟显示拖到 265 ms，窗口捕获 2.05 ms，记录在 bench/w13.json，没有冒充通过。

## W2 交付记录（2026-10-31）

- 3f4e584：CTranslate2 4.8.4 + oneDNN，NLLB int8；术语保护、占位符重试、批量、兜底原因。
- golden/nmt 120 条，tools/compare_nmt.py **120/120** 一致。
- C++ 测试 131 用例全过；模型加载 660.6 ms；4 句 490.9 ms。
- 裸模型质量差是预期；顺序始终是语料 → 规则 → 模型，语料命中不改写。

## W3 交付记录（2026-10-31）

| 提交 | 内容 |
| --- | --- |
| 461f161 | W3.0 原生 Win32 分层悬浮窗；点击穿透/置顶/不抢焦点 |
| d261fad | 修复 target 裁剪（测量与绘制统一字重）与默认 CLI 路径（同目录） |
| 1e8aa1a | W3.1 声明式呈现规格 + 400 ms 热重载 + 44 测试 |
| 62d6495 | W3.2 stdin 命令通道 + Ctrl+Alt+P/H/T + 托盘 + 234 用例 |
| 630af73 | W3.3 独立 tray owner 窗口 + finish() 写报告 + 240 用例 |

**W3.3 修复**：托盘图标与菜单现在属于独立的 1x1 WS_EX_TOOLWINDOW owner（屏幕外），
SetForegroundWindow 不再打在 WS_EX_NOACTIVATE 悬浮窗上；悬浮窗五个扩展样式不变。
run_overlay 统一用 finish() 处理托盘 Exit、WM_CLOSE、流结束和异常，报告包含
exit_reason、tray.added、hotkeys 实测值；Ctrl+C/关控制台会 post WM_CLOSE 并等最多 4 s，
强杀无法写报告，代码不假装。本 shell 无通知区（Shell_NotifyIcon error 5），托盘菜单仍需交互桌面复验。

## 环境坑（本机实测）

- **屏幕捕获 265 ms**：GameViewer 虚拟显示适配器；同尺寸窗口捕获 2.05 ms；DXGI 打不开。不要把 265 ms 当实现性能。
- **%TEMP% ACL**：Python 与测试进程写 %TEMP% 可能 Permission denied；W3.2 已把测试临时文件放 build 树。check_ocr.py 用 --out build-w11 或自动回退。
- **.git ACL**：shell 的 git add/commit 会 index.lock: Permission denied；TRAE agent 可以写。不要在 .git 里手动乱改；必须绕过时用临时 index/object + commit-tree，并核对 git show。
- **Clash**：系统代理可能关闭；Git 仓库本地配置 http.https://github.com.proxy=socks5h://127.0.0.1:7891。推送用 $env:TMP='E:\ProjectWatashi\.tmpgit' 避开 GCM 写 %TEMP% 的 ACL 问题。
- **Qt**：本机没有 Qt6，vcpkg 构建超出 timebox；ADR-0009 选 Win32 分层窗。
- **build/ 对 Python 不可写**；用 build-w11/build-w32 等。build-* 已 gitignore。
- **模型**：models/ 1.1 GB 已 gitignore；不要提交。

## 交接检查单

- [x] 工作区代码已提交；文档更新待提交
- [x] CI 能编译 C++（schema/stream 双校验）
- [x] 契约 schema 已导出
- [x] 黄金样本 ≥200；NMT 120 条
- [x] 模型哈希清单
- [x] 接手方能独立跑 prototype\run.cmd --selftest
- [x] 接手方能说出“哪些文档是过期的”
- [x] W3.3 托盘/报告修复（本 shell 正常 duration 报告通过；交互托盘待复验）
- [ ] W3.4 设置页
- [ ] W4 安装包/签名/8h soak
