# 安装、卸载与模型获取 / Install, uninstall and model fetching

## 1. 分发包是什么 / What ships

安装是 **per-user** 的：默认目录 `%LOCALAPPDATA%\ProjectWatashi`，不写
Program Files，不请求管理员，也不触发 UAC。

包内文件（见 [config/install_manifest.json](../config/install_manifest.json)）：

| 文件 | 用途 |
| --- | --- |
| `watashi_ui.exe` | 悬浮字幕窗（`--settings` 打开设置页） |
| `watashi_cli.exe` | 引擎（UI 用同目录的它） |
| `watashi_fetch.exe` | 模型获取器（本文件第 3 节） |
| `onnxruntime.dll` / `ctranslate2.dll` / `dnnl.dll` | 推理运行时 |
| MSVC/UCRT 运行时 DLL | 由 CMake `InstallRequiredSystemLibraries` 附带 |
| `models.lock.json` | 模型清单（URL、SHA-256、字节数、许可），**不是权重** |
| `config/presentation.json` | 呈现规格 |
| `config/settings_ui.json` | 由 `prototype/watashi/settings_schema.py` 生成的设置描述 |
| `LICENSE` / `THIRD_PARTY_LICENSES.md` | 许可 |

**不包含任何模型权重**。`models/` 由 W4.2 的获取器写入用户目录；安装包内没有
`.bin`/`.onnx`/Whisper/NLLB 权重。

用户数据在 `%LOCALAPPDATA%\ProjectWatashi\user`：`user.json`、语料、下载的模型。
卸载默认**保留**这个目录；只有显式 `-RemoveUserData` 才删除。

## 2. 安装与卸载 / Install and uninstall

本机没有 Inno Setup / NSIS / WiX，也无法离线获取，因此当前分发包是 **ZIP +
PowerShell 脚本**，不是 MSI/EXE 安装器：

```powershell
# 解压 ZIP 后，在解压目录运行
powershell -NoProfile -ExecutionPolicy Bypass -File .\install.ps1

# 卸载（开始菜单快捷方式和 HKCU 卸载项会在卸载时移除）
powershell -NoProfile -ExecutionPolicy Bypass -File "$env:LOCALAPPDATA\ProjectWatashi\uninstall.ps1"
```

脚本做的事：

- 复制清单中的文件到 `%LOCALAPPDATA%\ProjectWatashi`；
- 创建 `user\`（已存在则保留）；
- 复制 `uninstall.ps1`，记录 `installed_files.json`；
- 在 `HKCU\Software\Microsoft\Windows\CurrentVersion\Uninstall\ProjectWatashi`
  写 `DisplayName`、`DisplayVersion`、`InstallLocation`、`UninstallString` 等；
- 在 `%APPDATA%\Microsoft\Windows\Start Menu\Programs\Project Watashi` 创建快捷方式；
- 卸载时只删记录的文件、快捷方式和 HKCU 项，保留 `user\`（除非 `-RemoveUserData`）。

可自定义（测试与多份安装用）：

```powershell
.\install.ps1 -Prefix "$env:TEMP\watashi-test" -ShortcutDir "$env:TEMP\watashi-menu"
.\uninstall.ps1 -Prefix "$env:TEMP\watashi-test" -ShortcutDir "$env:TEMP\watashi-menu"
```

> 尚未做 MSI/EXE 安装器。若将来装入 Inno Setup，同一份清单和同一套
> install/uninstall 语义应保持不变。

## 3. 模型获取 / Fetching models

权重不随包分发，默认也**不自动下载**。只有显式 `--fetch` 才联网：

```powershell
# 只校验本地文件（大小 + SHA-256），不联网
.\watashi_fetch.exe --check --dest "$env:LOCALAPPDATA\ProjectWatashi\user"

# 下载缺失文件；断点续传；离线模式
.\watashi_fetch.exe --fetch --dest "$env:LOCALAPPDATA\ProjectWatashi\user"
.\watashi_fetch.exe --fetch --resume --dest ...
.\watashi_fetch.exe --fetch --offline --dest ...   # 只允许 file:// 镜像
```

默认 `--dest` 是 `%LOCALAPPDATA%\ProjectWatashi\user`，模型落在
`user\models\...`（与 `models.lock.json` 的 `models/` 前缀对应）。下载先写
`<file>.part`，校验字节数与 SHA-256 通过后才原子改名；**哈希不符会删除
`.part` 并报错**。

`models.lock.json` 的 `source` 对 Hugging Face 仓库会推导
`<source>/resolve/main/<file>`；OCR 三项来自 PyPI 的 `rapidocr-onnxruntime`
包内，没有可直接下载的单文件 URL，获取器会标为 `[manual]`——请从该 wheel 中
复制三个 ONNX 文件，或先安装 Python 包后按 `models.lock.json` 的路径放置。

### 许可 / Licences

- **NLLB-200-distilled-600M（CTranslate2 int8）：CC-BY-NC-4.0，非商业**。
  本项目是毕业设计、非商业开源；不得将本项目或其衍生作品用于商业目的。
  详见 [THIRD_PARTY_LICENSES.md](../THIRD_PARTY_LICENSES.md)。
- Whisper small：MIT。
- PP-OCRv4：Apache-2.0。

获取器在打印模型时会重复这些许可；`--fetch` 不会隐藏它们。

## 4. 日志、dump 与版本 / Logs, dumps and version

运行时数据都在 `%LOCALAPPDATA%\ProjectWatashi` 下（与 `-Prefix` 无关）：

| 路径 | 内容 |
| --- | --- |
| `logs\watashi.log` … `logs\watashi.4.log` | 环形日志：5 个文件 × 2 MiB，写满即轮转 |
| `dumps\*.dmp` | 本地 minidump，默认**不写**（见下） |
| `user\` | 用户配置、语料、下载的模型（卸载默认保留） |

- 日志只记录启动/退出、版本、退出码和错误摘要，**不记录字幕原文**。日志目录
  创建失败或写入失败时自动停用日志，不影响悬浮窗运行。
- minidump 由 `DbgHelp MiniDumpWriteDump` 在崩溃/未处理异常时写出，**只在本机**，
  不上传、不联网。因为 dump 可能包含当时屏幕上的文本，默认关闭；支持排障时
  显式开启：

```powershell
$env:WATASHI_DUMPS = "1"      # 仅本进程；会话级设置，重启后失效
.\watashi_ui.exe ...
```

- 版本信息：`watashi_ui.exe --version` / `watashi_cli.exe --version` /
  `watashi_fetch.exe --version` 输出产品版本、git commit 和构建类型；同样的
  信息写进启动日志和 `--report` 的 `product` 字段。

## 5. 签名 / Signing

包默认是 **unsigned**。仓库不含证书或密码：

```powershell
$env:WATASHI_SIGN_CERT = "C:\certs\watashi.pfx"
$env:WATASHI_SIGN_PASSWORD = "..."   # 可选，PFX 有密码时
powershell -NoProfile -ExecutionPolicy Bypass -File .\sign.ps1 -Path .\watashi_ui.exe, .\watashi_cli.exe, .\watashi_fetch.exe
```

`WATASHI_SIGN_CERT` 未设置、证书不存在或 `signtool.exe` 不在 PATH 时，脚本打印
`UNSIGNED` 警告并以 0 退出（CI 跳过签名）；`-RequireSignature` 会以 2 退出。
它**不会**把未签名文件报告成已签名。

## 6. 验收环境说明 / What was and was not verified here

2026-11-01 在本机 `build-w4` 上实际执行并记录：

- ZIP（`ProjectWatashi-0.1.0-win64.zip`，16.4 MB，无权重）解压到临时目录，
  `install.ps1` 安装到临时前缀（普通用户，无 UAC）→ `watashi_ui.exe --settings`
  打开设置窗并跑完 3 秒 synthetic 流（退出码 0，`reason=duration`，
  `click_through=pass`）→ `uninstall.ps1` 卸载；payload、快捷方式、
  HKCU 卸载项均被移除，`user\user.json` 保留。全程无管理员。
- 验收中发现并修复了一个真实安装 bug：`install.ps1` 在 `-File` 调用下
  `$PSScriptRoot` 未在参数默认值阶段绑定，导致 `SourceDir` 为空；现在在脚本体内
  解析，ZIP 内直接运行即可，不需要额外传参。
- `ctest --test-dir build-w4 -C Release --parallel 4`：**302/302 通过**（含
  20 个 W4.1/W4.2 用例、9 个 W4.3 单元用例、`ui_version`/`cli_version`/
  `fetch_version`）。其中
  “hash mismatch deletes the partial file”在个别会话中会因宿主的文件过滤
  （`DeleteFileW` 返回成功但文件仍在）以 **77 SKIP**；本次全量运行通过。
- `watashi_fetch --check`（缺模型）返回 1；`--fetch --offline` 拒绝全部 http
  下载并返回 1，未发起网络请求。
- W4.3：日志环形轮转（5×2 MiB）、minidump 文件写入、`--version` 字段均有
  单元测试；本机的宿主过滤禁止原生进程创建 `%LOCALAPPDATA%\ProjectWatashi`，
  因此验收时先建好 `logs` 目录再运行，日志正常写入（含版本与退出码）。
  在普通桌面没有这个限制。
- W3.3 托盘：本次运行打印 `tray: version 4 enabled` 与 `tray: icon added`，
  **通知区正常**；`tray-exit` 需要真实点击托盘菜单，未自动化执行，标记为
  **交互未验**（未宣称通过）。
- 受限令牌宿主（`NT AUTHORITY\RESTRICTED`，SID `S-1-5-12`）会拒绝 HKCU 写入；
  这时 `installer_install` / `installer_remove_user_data` 以 **77** 退出并被
  CTest 标记为 **SKIP**，不会显示为 pass。普通桌面上可直接运行：

```powershell
# 解压后，普通用户、不要“以管理员身份运行”
powershell -NoProfile -ExecutionPolicy Bypass -File .\install.ps1
powershell -NoProfile -ExecutionPolicy Bypass -File "$env:LOCALAPPDATA\ProjectWatashi\uninstall.ps1"
```

- **未在干净 Win11 VM 上验证**：当前环境没有可用的干净 VM。
- 签名脚本在无证书环境下运行，产物明确标记为 unsigned（未冒充已签名）。
