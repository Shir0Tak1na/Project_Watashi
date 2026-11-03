# ADR 0010：控制面回退（ControlSurface）/ Control-surface fallback

- 状态：Accepted（v1.0.0-rc1 之后，未升版本）
- 日期：2026-11-03
- 关联：W3.3 托盘、`--control`、ControlWindow、Ctrl+Alt+Q/C

## 背景 / Context

托盘图标是 W3.3 的默认控制入口，但在部分 Medium 桌面/会话上并不可靠：

- `Shell_TrayWnd` 与 `TrayNotifyWnd` 存在，但 `ToolbarWindow32` 子窗口为 0；
- `Shell_NotifyIcon(NIM_ADD)` 返回 **error 5**（access denied）；
- 最小化的 NotifyIcon 探针（不经过本项目代码、976 字节
  `NOTIFYICONDATA`、内置 `STATIC` owner）同样失败；
- 同一台机器上，某些会话可以 `tray: icon added`，另一些会话持续 error 5。

因此托盘不是可保证的发布入口。继续把它当唯一控制面，会让“能安装、能跑”的
机器因为通知区状态而无法暂停/隐藏/退出。

## 决策 / Decision

1. **控制面抽象**：`ControlAction`（None/Pause/Visibility/Exit）是唯一动作表，
   `control_action_from_id()` 同时映射托盘菜单 id、控制窗按钮 id 与热键 id。
2. **可选控制面**：启动参数 `--control auto|tray|window|none`，默认 `auto`：
   - `auto`：先 `tray.add()`；成功则不建窗口，失败则显示 ControlWindow；
   - `tray`：只试托盘，失败仍继续（仅热键），不建窗口；
   - `window`：强制 ControlWindow，不注册托盘；
   - `none`：只用热键。
3. **ControlWindow**：普通 Win32 顶层窗口（标题 `watashi control`，工作区右下角），
   三个按钮 Pause/Resume、Hide/Show、Exit；关闭按钮只隐藏；`SW_SHOWNOACTIVATE`
   显示，不抢焦点。
4. **热键**：保留 Ctrl+Alt+P/H/T，新增 Ctrl+Alt+Q（退出）与 Ctrl+Alt+C
   （显示/隐藏控制窗）；注册失败只日志。
5. **报告**：`control_surface=tray|window|hotkeys`、`tray.added=true/false`、
   `exit_reason=tray-exit|control-exit|hotkey-exit|...`，便于验收区分实际路径。

## 后果 / Consequences

- 托盘成为 **best-effort**：可用时仍是首选；不可用时不再阻塞发布。
- 没有通知区的机器仍可完整控制（ControlWindow + 热键）。
- 托盘菜单与控制窗共用 `ControlAction`，不会出现两套行为。
- 本 ADR **不声称托盘 PASS**；托盘 T1/T2/T3 仍需在可用通知区的桌面单独验收。

## 证据 / Evidence（2026-11-03）

- `Shell_NotifyIcon(NIM_ADD)` → `tray: NIM_ADD failed ... error=5`。
- 最小探针同样失败；探针不引用 watashi 代码。
- 因此本机托盘路径记为 known issue/未验，控制面验收以 ControlWindow/热键为准。
