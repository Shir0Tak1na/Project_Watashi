# Third-party licenses / 第三方许可

> 本仓库的**代码**是 MIT（见 [LICENSE](LICENSE)）。**MIT 不覆盖模型权重**，也不覆盖下表的第三方组件——它们各自保留原许可。
> 复核日期：2026-10-28。版本来自本机 `.venv` 实测，许可字段来自包元数据自述，**以各项目 LICENSE 原文为准**。

## 1. 本仓库代码

MIT © 2026 Shir0Tak1na。见 [LICENSE](LICENSE)。

## 2. 模型权重（不随仓库分发）

| 资产 | 许可 | 用途 | 分发方式 |
| --- | --- | --- | --- |
| NLLB-200-distilled-600M（CTranslate2 int8，约 618 MB） | **CC-BY-NC-4.0** | 翻译兜底 | `fetch_model` 下载；**非商业** |
| Whisper small（CTranslate2 int8，约 464 MB） | MIT | 语音识别 | `fetch_model --asr` 下载 |
| PP-OCRv4 det / cls / rec（ONNX，约 16 MB） | Apache-2.0 | 屏幕 OCR | 随 `rapidocr-onnxruntime` 安装包分发 |

**本项目是毕业设计，非商业开源。** 由于 NLLB 是 CC-BY-NC-4.0，任何使用者都不得将本项目或其衍生作品用于商业目的。若将来需要商用：替换模型并重跑黄金样本，见 [docs/cpp-v1-plan.md](docs/cpp-v1-plan.md) 的风险 R5。

CC-BY-NC-4.0 的义务：署名、附许可链接、标注是否修改。

## 3. Python 运行时依赖（实测版本）

| 包 | 版本 | 许可 |
| --- | --- | --- |
| rapidocr-onnxruntime | 1.4.4 | Apache-2.0 |
| onnxruntime | 1.30.0 | MIT |
| opencv-python | 5.0.0.93 | Apache-2.0 |
| numpy | 2.5.3 | BSD-3-Clause（含 0BSD / MIT / Zlib / CC0 片段） |
| mss | 10.2.0 | MIT |
| pillow | 12.3.0 | MIT-CMU |
| pyyaml | 6.0.3 | MIT |
| fastapi | 0.141.1 | MIT |
| uvicorn | 0.52.4 | BSD-3-Clause |
| soundcard | 0.4.6 | BSD-3-Clause |
| faster-whisper | 1.2.1 | MIT |
| ctranslate2 | 4.8.2 | MIT |
| sentencepiece | 0.2.2 | Apache-2.0 |
| tokenizers | 0.23.2 | Apache-2.0 |
| requests | 2.34.2 | Apache-2.0 |
| python-xlib | 未安装（仅 Linux） | LGPL-2.1 |

Python 3.12 本身为 PSF-2.0；`tkinter` 随 Python 分发，不单独列出。

## 4. C++ 目标依赖（规划中，尚未引入）

| 组件 | 许可 | 链接方式 |
| --- | --- | --- |
| ONNX Runtime | MIT | 动态库 |
| CTranslate2 | MIT | 静态或动态 |
| Qt 6 | LGPL-3.0 | **仅动态链接**，随包附许可与替换说明 |
| SQLite | Public Domain | 静态 |
| OpenCV | Apache-2.0 | 动态 |
| nlohmann/json | MIT | 头文件 |
| vcpkg 传递依赖 | 各自原许可 | 由 vcpkg 清单锁定版本 |

> Qt 若改为静态链接，LGPL 的合规成本会显著上升（需要提供重链接能力）。v1.0 按动态链接执行，见规划 ADR-003。

## 5. 分发规则（本项目的自我约束）

1. 权重不进 git（已由 `.gitignore` 排除）、不进安装包；由 `fetch_model` 在首次运行前说明来源与体积后下载。
2. 仓库与发行包内同时放置 `LICENSE`（MIT）与本文件。
3. 不使用"整个项目是 MIT"的表述；README 的许可段落写明边界。
4. NLLB 仅限非商业；商业化之前必须替换并复核。

## 6. 如何复核

```bash
.venv/Scripts/python.exe -c "import importlib.metadata as m; print(m.metadata('onnxruntime')['License'])"
```

或逐个 `pip show`。元数据里的许可字段可能为空或过时，最终以 upstream 的 LICENSE 原文为准。
