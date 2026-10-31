# 0009 原生悬浮窗的 UI 技术选型

- 状态：已接受（W3.0）
- 日期：2026-10-31
- 范围：最小悬浮窗（`watashi_ui.exe`）。设置页、托盘、桌面主窗不在本决策内。

## 背景

W3.0 只需要一个能看字幕的窗口，但它的要求恰好是普通控件窗口最难满足的一组：

- 无边框、逐像素 alpha、始终置顶；
- 点击穿透，鼠标事件落到下面的游戏/窗口；
- 显示时不抢焦点（不激活、不进 Alt-Tab、不进任务栏）；
- 底边居中锚点，支持多行；
- PerMonitorV2 DPI，100% / 125% / 150% 位置与字号都对；
- 数据只来自 `watashi_cli.exe --json-lines`，UI 不复制引擎逻辑。

Phase 0 的 timebox 是 1 天：先在 Qt6 与 Win32 分层窗之间做实测，Qt6 不收敛就转
Win32，并把原因写在这里。

## 实测记录

| 探针 | 结果 |
| --- | --- |
| `where vcpkg` / `$env:VCPKG_ROOT` | 都没有；`C:\vcpkg`、`C:\Qt`、`Program Files\Qt` 不存在 |
| `find_package(Qt6 ...)` | 找不到 Qt6 CMake 包 |
| `github.com` git clone | 反复 reset，无法稳定拉取 qtbase 及子模块 |
| `codeload.github.com` | 可达（已用于本项目的 nlohmann/Catch2/CTranslate2/SentencePiece 下载） |
| Qt 在线安装器 | 链接 404，无法作为可靠安装路径 |
| vcpkg 从源码构建 qtbase | 需要先引导 vcpkg、下载并编译 Qt Base 及其依赖（GB 级、小时级），超出 1 天 timebox 且本阶段不需要 |

结论：Qt6 在本机不可用；"从源码用 vcpkg 编译 Qt"是唯一可行路径，但它为一个最小
悬浮窗引入的构建时间和分发体积都不成比例。按任务约定，停 Qt，转 Win32。

## 决策

采用 **Win32 分层窗口 + GDI 文字 + 手写合成**，不引入任何 UI 框架。

窗口标志（[layered_window.cpp](../../src/ui/layered_window.cpp)）：

```
WS_POPUP
WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_TOPMOST |
WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW
```

逐像素 alpha 由 `UpdateLayeredWindow(..., ULW_ALPHA)` 提交，缓冲区是预乘 BGRA。
点击穿透有两层保证：扩展样式 `WS_EX_TRANSPARENT`，以及窗口过程对
`WM_NCHITTEST` 返回 `HTTRANSPARENT`、对 `WM_MOUSEACTIVATE` 返回 `MA_NOACTIVATE`。
不抢焦点由 `WS_EX_NOACTIVATE` + `SW_SHOWNOACTIVATE` 保证。

文字不能直接画进图层缓冲：GDI 只会写不透明像素。因此文字先画到一张黑白覆盖率
掩码 DIB，再按覆盖率把颜色合成进预乘缓冲区。这一步是手写的，所以抗锯齿、颜色和
透明度的结果与机器无关，也不需要 Direct2D。

几何全部在 [layout.hpp](../../include/watashi/ui/layout.hpp) 中，纯函数、注入文字度量：
DPI 换算、贪心换行、底边居中锚点都能在无显示器的机器上断言。窗口只负责提交像素。

## 被否方案

| 方案 | 否决原因 |
| --- | --- |
| Qt6 Widgets（LGPL 动态链接） | 本机无 Qt、无 vcpkg；源码构建超出 timebox。若将来重开：必须动态链接并保留重链接能力，随包附 LGPL 文本与源码获取方式；静态链接会让合规成本显著上升，不接受 |
| Qt6 静态链接 | 与 LGPL 的"可重新链接"要求冲突，且体积更大 |
| 把 Python/tkinter 悬浮窗嵌进来 | 任务明确禁止；且会把第二个运行时和进程胶水塞进 C++ 交付物 |
| WebView2 / Electron / DirectComposition | 为字幕条引入浏览器运行时或额外图形栈，超出 W3.0 范围 |
| 普通无边框窗口 + `SetLayeredWindowAttributes` | 只能整窗统一透明度，做不到逐像素 alpha 与干净的文字边缘 |

## 依赖与许可

| 项 | 版本 | 许可 | 说明 |
| --- | --- | --- | --- |
| USER32 / GDI32 / PSAPI | 随 Windows | 系统 API | 不随包分发，无新增第三方依赖 |
| 字体 | Microsoft YaHei UI（系统字体） | 系统组件 | 只按名字引用，不打包字体文件 |
| Qt6（未采用） | — | LGPL-3.0（动态链接） | 见上表；本阶段不引入 |

本决策没有新增 `vcpkg.json` 依赖，也没有新增需要随包分发的 DLL。

## 后果

- UI 只支持 Windows。项目本来就是 Windows 优先（GDI 捕获、DWM、ONNX Runtime
  win-x64），这个限制在 v1.0 内可接受。
- DPI 必须在进程启动、创建任何窗口之前切到 PerMonitorV2；`watashi_ui` 调用了
  引擎已有的 `enable_per_monitor_dpi()`。
- 设置页、托盘、桌面主窗如果以后要做，Win32 的代码量会明显上升。那时应新开一条
  ADR 重新评估 UI 工具包（Qt 仍在候选内），而不是把本决策外推。
- 悬浮窗是只读视图：它不持有 OCR、语料或 NMT 代码，数据只经过
  [model.cpp](../../src/ui/model.cpp) 的事件解码。
- `watashi_ui` 默认在**自身目录**找 `watashi_cli.exe`（两者总被同一个构建目标输出到
  同一目录），不写死任何 build 目录；干净检出里不存在旧引擎时由 `--cli` 显式指定。
- 文字必须"按什么字重测量、就按什么字重绘制"：target 用 semibold，布局就用 semibold
  度量（[layout.hpp](../../include/watashi/ui/layout.hpp)）。用 regular 测量再按
  semibold 合成会让行宽小于字形，右边缘被裁掉。
