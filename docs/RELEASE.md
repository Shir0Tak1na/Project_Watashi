# 发布验收清单 / Release checklist

适用对象：**干净 Win11 VM 上的普通用户桌面**（per-user 安装，不需要管理员，
不触发 UAC）。与 [docs/INSTALL.md](INSTALL.md) 互补：本文是发布前逐项检查表，
INSTALL.md 是使用者安装说明。

状态日期：2026-11-01（**v1.0.0-rc1**）。

## 0. 当前验证状态 / Status

| 项目 | 状态 |
| --- | --- |
| 10 分钟 UI soak（synthetic/fake） | **PASS**（2026-11-01 clean-room，`build-rc/soak-10m.json`：602.5s，handles 169→244（退出前最后采样），内存 14.9 MiB，峰值 21.1 MiB，exit 0，log_errors 0） |
| 8 小时 soak（`--duration 28800`） | **进行中**：2026-11-01 20:43 启动，PID 5224，报告 `build-rc/soak-8h.json` 尚未生成；完成前不得写成通过 |
| 干净 Win11 VM | **未验证**：当前没有可用 VM |
| 普通（Medium）桌面手工安装（W4.1） | **待办**：自动化在 Low IL 宿主会 skip，必须人工验收 |
| W3.3 托盘 `tray-exit` | **未验证**：需要真实点击托盘菜单 |
| 签名 | **unsigned**：仓库无证书；`sign.ps1` 只会在有证书时签名 |

> **skipped ≠ passed**：CTest 中 `installer_install` /
> `installer_remove_user_data` 在 Low IL（`S-1-16-4096`）宿主会以 77 退出并被
> 标记为 skipped；这只说明宿主不能写 HKCU，不是安装通过。

### RC1 产物 / RC1 artefacts

- `dist/ProjectWatashi-1.0.0-rc1-win64.zip`（约 16.4 MB）；
  `dist/SHA256SUMS.txt` 记录归档的 SHA-256；具体字节数与哈希在打包输出中记录。
- 解压审计（`build-rc/unzip-check`）：`install_manifest.json` 的 15 项 payload
  全部存在；`.bin`/`.onnx`/`.pt`/`.pth`/`.safetensors` 为 **0**；没有 `models/`；
  `.pdb` 为 **0**；ZIP 哈希与 `SHA256SUMS.txt` 一致。
- ZIP 不进仓库（`dist/` 已在 `.gitignore`）。

### clean-room 复验 / Clean-room rebuild (2026-11-01)

- `git archive HEAD` → `build-rc/clean-src`，在 `build-rc/clean-build` 重新
  configure/build；`ctest --parallel 4` → **313/313 通过**。
- `cmake --install` 到 `build-rc/clean-stage` 与
  `dist/ProjectWatashi-1.0.0-rc1-win64.zip` 逐文件对比：
  - 文件列表 **26/26 一致**，无缺失/多余；
  - **9 个文本文件**（`install.ps1`、`uninstall.ps1`、`install_manifest.json`、
    两份 `LICENSE`、两份 `THIRD_PARTY_LICENSES.md`、`MODEL_LICENSES.md`、
    `config/settings_ui.json`）字节不同：工作区检出为 CRLF，`git archive` 为 LF；
    **归一化行尾后内容完全一致**；
  - **4 个二进制**（`watashi_ui.exe`、`watashi_cli.exe`、`watashi_fetch.exe`、
    `ctranslate2.dll`）尺寸相同但 SHA-256 不同（构建非确定性元数据；未嵌入源码
    路径）。其余二进制（ORT/oneDNN/VC runtime）与 SBOM 等逐字节一致；
  - **结论：文件集合与尺寸一致，但不是逐字节可重现**；按任务要求如实记录，
    不写成“ZIP 完全一致”。
- 权重扫描：`clean-stage` 与 ZIP 均为 **0**，无 `models/`、无 `.pdb`。

## 1. 安装 / Install

在干净 VM 上，用**普通用户**、不要“以管理员身份运行”：

- [ ] 解压 `dist/ProjectWatashi-1.0.0-rc1-win64.zip`，确认里面**没有** `.bin` / `.onnx` /
      `.pt`，没有 `models/` 目录；
- [ ] 确认 `licenses/` 含 `LICENSE`、`THIRD_PARTY_LICENSES.md`、
      `MODEL_LICENSES.md`、`sbom.spdx.json`；
- [ ] 运行 `install.ps1`，检查：
  - `%LOCALAPPDATA%\ProjectWatashi` 下有 exe、DLL、`config\`、`licenses\`；
  - `user\` 已创建；
  - `HKCU\Software\Microsoft\Windows\CurrentVersion\Uninstall\ProjectWatashi`
    写入 `DisplayName` / `DisplayVersion` / `InstallLocation` / `UninstallString`；
  - 开始菜单出现 `Project Watashi` 快捷方式；
- [ ] `watashi_ui.exe --version` 输出产品版本、git commit、build type；
- [ ] `watashi_ui.exe --settings` 能打开设置页（字段来自
      `config/settings_ui.json`）。

## 2. 运行 / Run

- [ ] 用 synthetic/fake 跑一次短会话（不需要模型）：
      `watashi_ui.exe --capture synthetic --ocr fake --duration 60`
- [ ] 检查悬浮窗、点击穿透、`--report` 的 `product` / `exit_reason`；
- [ ] `%LOCALAPPDATA%\ProjectWatashi\logs\watashi.log` 有 start/exit 行，
      且**没有字幕原文**；
- [ ] 通知区可用时确认 `tray: icon added`；`tray-exit` 需真实点击菜单，
      未验证时如实记录。

## 3. 模型 / Models

- [ ] 先验证缺失模型的行为：`watashi_fetch.exe --check --dest <user>` → 退出码
      **1**，不联网；
- [ ] 只在明确需要时执行 `watashi_fetch.exe --fetch --dest <user>`（这是唯一
      会联网的步骤，需另行确认网络与体积）；
- [ ] 校验通过后模型落在 `user\models\...`，安装包内始终没有权重；
- [ ] 阅读并确认许可：代码 **MIT**；NLLB-200-distilled-600M 为
      **CC-BY-NC-4.0（非商业）**；Whisper small **MIT**；PP-OCRv4
      **Apache-2.0**。见 `licenses/MODEL_LICENSES.md` 与
      `THIRD_PARTY_LICENSES.md`。

## 4. 卸载 / Uninstall

- [ ] 先写入一点用户数据（如 `user\user.json`、语料）；
- [ ] 运行 `uninstall.ps1`（开始菜单快捷方式或 HKCU 的 UninstallString 都可）；
- [ ] 检查：payload、`licenses\`、快捷方式、HKCU 卸载项全部移除；
- [ ] `user\` **默认保留**；只有 `-RemoveUserData` 才删除；
- [ ] 前缀中只剩 `user\` 时，前缀目录保留（这是预期行为）。

## 5. Soak / Long-run

短测由 CTest 覆盖（`soak_bad_args`、`soak_short_ui`、`soak_short_cli`、
`soak_missing_exe`），10 分钟与 8 小时是**手工**运行：

```powershell
# 10 分钟 smoke（发布前必须）
powershell -NoProfile -ExecutionPolicy Bypass -File tools\soak.ps1 `
  --duration 600 --mode ui --report build-rc\soak-ui-10m.json

# 8 小时（当前未跑；跑完才可把状态改成 pass）
powershell -NoProfile -ExecutionPolicy Bypass -File tools\soak.ps1 `
  --duration 28800 --mode ui --report build-rc\soak-ui-8h.json
```

报告 JSON 至少包含：`requested_duration_seconds`、`elapsed_seconds`、
`handles_start` / `handles_end`、`memory_mib_start/end/peak`、`exit_code`、
`crashed`、`log_errors`、`pass`、`failure_reason`。harness 不联网、不加载模型。

- [ ] 10 分钟 smoke `pass=true`（把本文件第 0 节状态改为实际结果）；
- [ ] 8 小时：**未跑** → 保持“未验证”，不得写成通过。

## 6. 签名 / Signing

- [ ] 无证书时明确标记 **unsigned**（当前发布就是 unsigned）；
- [ ] 有证书时设置 `WATASHI_SIGN_CERT`（密码可选 `WATASHI_SIGN_PASSWORD`），
      运行 `installer\sign.ps1`，确认 `SIGNED` 与 signtool 校验通过；
      仓库内不得出现证书/密码。

