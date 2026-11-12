# 交接说明 / Handover

> 给接手的人或编码代理：**先读这一页，再读别的。**
> 最后更新：**2026-11-12（v1.0.0 已 push/备份；P0 剩余实际 fetch、backend/trace、真实目录卸载）**

## 一句话状态

C++ 主线 **v1.0.0 已发布**：B 验收完成（Medium 桌面、干净 Win11 VM、控制面、
安装/卸载、10 分钟与 8 小时 soak），clean build 318/318，最终包
`dist/ProjectWatashi-1.0.0-win64.zip`（SHA-256 与审计见 docs/RELEASE.md），
annotated tag `v1.0.0`（deref `a0ecccd`）**已 push**，包已备份到
`D:\WatashiRelease\v1.0.0\`（副本校验通过）。托盘在本机不可用，为 best-effort，
控制窗/热键是已验收的发布入口。

## 必读顺序（约 30 分钟）

1. [docs/ACCEPTANCE.md](docs/ACCEPTANCE.md) — 本次人工验收的真实结果与证据
2. [docs/POST-V1.0.md](docs/POST-V1.0.md) — 发布后 P0/P1/P2 计划
3. [docs/RELEASE.md](docs/RELEASE.md) — 发布状态、包、已知限制
4. [docs/cpp-v1-plan.md](docs/cpp-v1-plan.md) — 执行计划；先看 §0
5. [README_zh.md](README_zh.md) / [README.md](README.md) — 实际能力与构建
6. [prototype/README.md](prototype/README.md) — Python 参考实现
7. [prototype/watashi/events.py](prototype/watashi/events.py) — 事件/命令契约，冻结对象
8. [THIRD_PARTY_LICENSES.md](THIRD_PARTY_LICENSES.md) — 许可边界

## 什么是真的（可复现）

- **C++ 测试**：clean build（git archive 版本 commit `7a2c1a0`）通过 **318/318 CTest**；低完整性宿主上 installer 两例会以 77 skip（本机实际执行通过）。
- **控制面**：`--control auto` 在 tray 不可用时自动显示 `watashi control`；Pause/Hide/Exit 已验。
- **安装/卸载**：标准用户、无管理员；HKCU + 开始菜单；卸载默认保留 `user\`。
- **8h soak**：28801.5 s，exit 0，handles 169→169，内存 15.0→13.8 MiB（peak 16.4），log_errors 0。
- **vcomp**：CTranslate2 依赖 `VCOMP140.DLL`；包内已包含，干净 VM 不再 `0xC0000135`。
- **NMT 差分**：120/120 与 Python 参考逐字段一致。
- **OCR**：50 合成帧 + 10 受控字幕，均值/中位数/最小 1.0000；1200×200 P50 49–52 ms。
- **捕获**：窗口 P50 2.05 ms；屏幕 P50 264.6 ms 是虚拟显示环境限制。

## 什么是过期的（别照着做）

| 位置 | 写着 | 实情 |
| --- | --- | --- |
| 旧 README 状态/W3.3 | W3.4/W4 未开始 | 已发布 v1.0.0；以本页和 ACCEPTANCE 为准 |
| docs/RELEASE 旧段 | RC1 未验收/无 VM | 已于 2026-11-11 完成 B 验收并发布 v1.0.0 |
| config/app.yaml | C++ 主配置 | C++ 不读它；设置页从 config/settings_ui.json 读取 |
| docs/architecture.md | UI 不链接 engine | 当前 CMake 仍为取事件解码/DPI 链接 `watashi_core`；这是待偿还的架构债，不要当成已实现边界 |

## 硬约束（违反即返工）

1. 事件 JSON 与 events.py 逐字段一致（v/seq/ts 信封）。
2. 设置项键名/默认值来自 settings_schema.py；W3.4 已落地。
3. 语料命中即权威；未命中落模型必须报告兜底原因。
4. v1.0 只允许本地后端；外部/局域网默认关闭，需先有 ADR。
5. 模型权重不进 git、不进安装包。
6. UI 只消费 watashi_cli --json-lines，命令只走 stdin；当前 CMake 的 UI→core 链接是待还债，不是新许可。

## 当前状态（2026-11-12）

| 项 | 状态 |
| --- | --- |
| 版本 | **v1.0.0**（annotated tag 已 push；tag 前最后版本 commit `7a2c1a0`） |
| 远端 | **已 push（2026-11-12）**：`origin/main` = `4ea7a2a`，tag `v1.0.0`（`50f5aab` → `a0ecccd`），**tag 未移动、无 force** |
| 工作区 | clean；build-*/dist/ 已 gitignore |
| 测试 | clean build（git archive 7a2c1a0）**318/318 CTest**（installer 两例本机实际执行）；license/SBOM/soak 检查 |
| 验收 | Medium PASS；VM PASS（BIOS/TPM 绕过）；控制面 PASS；安装/运行 PASS；真实目录卸载未验（备用 prefix PASS） |
| 长稳 | 10m PASS；8h PASS（`build-rc/soak-8h.json`） |
| **最终包** | **`dist/ProjectWatashi-1.0.0-win64.zip`**，16,483,547 字节，SHA-256 `da6f0c99...`（完整值/审计见 docs/RELEASE.md §1；`dist/SHA256SUMS.txt` `sha256sum -c` OK） |
| 备份 | **已备份（2026-11-12）**：`D:\WatashiRelease\v1.0.0\`（ZIP + SHA256SUMS，副本 `sha256sum -c` OK，哈希与 `dist/` 一致） |
| GitHub Release | 未创建（无 `gh`/token）；手工上传步骤见 docs/RELEASE.md §10.3 |
| 验收包 | `build-rc/stage-vcomp.zip`（RC1 B 验收对象，16,943,345 bytes） |
| 旧包 | `build-rc/archive/rc1/ProjectWatashi-1.0.0-rc1-win64.zip`（归档） |
| 托盘 | 本机/VM 通知区不可用；best-effort，不阻塞发布 |
| 签名 | unsigned |
| 下一步 | P0 剩余：实际 `--fetch`、`ppocr_keys_v1.txt`、`backend`/`trace` 写死、真实目录卸载、GitHub Release；见 [docs/POST-V1.0.md](docs/POST-V1.0.md) |

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
| 524a79a | W3.3 icon owner=overlay、menu owner 独立 + 242 CTest；托盘交互待干净桌面 |
| 06855cd | W3.4 schema 驱动设置窗 + user.json 持久化 + 270 CTest |

**W3.3 修复**：icon owner 用悬浮窗 hwnd，menu owner 用独立的 1x1 可激活窗口；
`SetForegroundWindow`/`TrackPopupMenu` 只打 menu owner，悬浮窗的 NOACTIVATE 不变。
run_overlay 统一用 finish() 处理托盘 Exit、WM_CLOSE、流结束和异常，报告包含
exit_reason、tray.added、hotkeys 实测值。当前会话 raw `Shell_NotifyIcon(NIM_ADD)`
与 C# 直调都返回 error 5，说明该会话通知宿主不接受 NIM_ADD；托盘菜单需在干净通知区桌面复验，不能据此判代码失败。

## 环境坑（本机实测）

- **屏幕捕获 265 ms**：GameViewer 虚拟显示适配器；同尺寸窗口捕获 2.05 ms；DXGI 打不开。不要把 265 ms 当实现性能。
- **%TEMP% ACL**：Python 与测试进程写 %TEMP% 可能 Permission denied；W3.2 已把测试临时文件放 build 树。check_ocr.py 用 --out build-w11 或自动回退。
- **.git ACL**：shell 的 git add/commit 会 index.lock: Permission denied；TRAE agent 可以写。不要在 .git 里手动乱改；必须绕过时用临时 index/object + commit-tree，并核对 git show。
- **Clash**：系统代理可能关闭；Git 仓库本地配置 http.https://github.com.proxy=socks5h://127.0.0.1:7891。推送用 $env:TMP='E:\ProjectWatashi\.tmpgit' 避开 GCM 写 %TEMP% 的 ACL 问题。
- **Qt**：本机没有 Qt6，vcpkg 构建超出 timebox；ADR-0009 选 Win32 分层窗。
- **build/ 对 Python 不可写**；用 build-w11/build-w32 等。build-* 已 gitignore。
- **模型**：models/ 1.1 GB 已 gitignore；不要提交。

## 交接检查单

- [x] 工作区代码已提交；文档已更新到 2026-11-11 v1.0.0 发布状态
- [x] CI 能编译 C++（schema/stream 双校验）
- [x] 契约 schema 已导出
- [x] 黄金样本 ≥200；NMT 120 条
- [x] 模型哈希清单
- [x] 接手方能独立跑 prototype\run.cmd --selftest
- [x] 接手方能说出“哪些文档是过期的”
- [x] 控制面：ControlWindow/热键（tray 在本机不可用，best-effort；控制窗 Pause/Hide/Exit PASS）
- [x] W3.4 设置页（已落地；Medium/VM 设置页打开已验）
- [x] W4 安装包/模型获取/SBOM/日志/10 分钟与 8 小时 soak（8h 报告
      `build-rc/soak-8h.json` PASS；vcomp140 打包已修；unsigned）
- [x] 人工验收：Medium 桌面、干净 Win11 VM、安装/卸载（2026-11-11 PASS）
- [ ] 托盘实点：本机/VM 通知区不可用；control surface 已 PASS，tray 仍为 best-effort
