# 模型许可 / Model licences

> 这些模型**不随安装包分发**，也没有被打进 SBOM 的 payload。`models.lock.json`
> 记录每个文件的 SHA-256 与字节数；`watashi_fetch` 只在显式 `--fetch` 时下载。
> 复核日期：2026-11-12。

## NLLB-200-distilled-600M（CTranslate2 int8）

- 许可：**CC-BY-NC-4.0**（Creative Commons Attribution-NonCommercial 4.0）
- 许可原文：https://creativecommons.org/licenses/by-nc/4.0/
- 用途：翻译兜底（`--translator nmt/hybrid`）
- 分发：`watashi_fetch --fetch` 从 Hugging Face 下载；`models.lock.json#nllb-200-distilled-600M-ct2-int8`
- **非商业限制**：本项目是毕业设计、非商业开源。任何使用者不得将本项目或其衍生
  作品用于商业目的。CC-BY-NC-4.0 还要求署名、附许可链接并标注是否修改。
- 商业化之前必须替换模型并重跑黄金样本，见 `docs/cpp-v1-plan.md` 风险 R5。

## Whisper small（CTranslate2 int8）

- 许可：**MIT**
- 来源：https://huggingface.co/Systran/faster-whisper-small
- 用途：语音识别（W4 之后接入）
- 分发：`watashi_fetch --fetch` 下载；`models.lock.json#whisper-small-ct2-int8`

## PP-OCRv4（ONNX）

- 许可：**Apache-2.0**
- 来源：`rapidocr-onnxruntime` 1.4.4 官方 PyPI wheel（项目主页：
  https://github.com/RapidAI/RapidOCR ；发布元数据：
  https://pypi.org/pypi/rapidocr-onnxruntime/1.4.4/json ）。PyPI 与 wheel 的
  `METADATA` 均声明 `License: Apache-2.0`。
- wheel：`rapidocr_onnxruntime-1.4.4-py3-none-any.whl`，14,915,192 字节，
  SHA-256 `971d7d5f223a7a808662229df1ef69893809d8457d834e6373d3854bc1782cbf`；
  不可变下载 URL 记录在 `models.lock.json#pp-ocrv4-onnx` 的三个文件条目中。
- 用途：屏幕 OCR。
- 分发：`watashi_fetch --fetch` 下载并校验官方 wheel，从
  `rapidocr_onnxruntime/models/` 自动提取 det/rec/cls 三个 ONNX；每个提取文件再按
  lock 中的大小和 SHA-256 独立校验。wheel 仅为运行期临时缓存，成功后清理；
  安装包内没有 `.onnx` 或 wheel。

## 代码许可

- 本项目**代码**是 MIT（见 `licenses/LICENSE`）。
- 第三方组件许可见 `licenses/THIRD_PARTY_LICENSES.md`。
- MIT **不覆盖**上述模型权重；每个模型保留自己的许可。
