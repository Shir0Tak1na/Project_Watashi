# Project Watashi（渡）

[English](README.md) | 简体中文

*实时屏幕区域翻译，专注网络用语与虚构词汇 —— 全本地部署。*

Project Watashi（渡）是一款桌面工具：持续识别用户在屏幕上圈定区域内的文字，并将其翻译为设定的目标语言。它针对的是通用翻译工具表现糟糕的场景——网络用语、修仙/魔幻黑话，以及科幻作品中自造的词汇。

主链路上的全部环节均在本机运行：不依赖云端 API，可完全离线工作。

## 核心功能

- **实时区域翻译** —— 圈定屏幕区域并选择语言对，悬浮窗以双语对照形式展示原文与译文，无需复制粘贴。
- **全本地部署** —— 屏幕捕获、OCR、语料库匹配、规则推演以及可选的 LLM 全部在本机运行。云端大模型 API 仅作为可选插件存在，且默认关闭。
- **自定义语料库** —— 通过编写语料库文件，即可扩展网络用语与虚构词汇的翻译覆盖范围。无需修改引擎，无需重新编译。
- **自定义规则** —— 语料库中缺失的词汇由用户自定义规则处理（词根词缀拆分、命名模板、音译转写、领域映射、词素组合）。每次规则命中都会返回所命中的规则标识与置信度，结果可追溯、可校对。
- **可选本地 LLM** —— 仅在语料库与规则均未命中时，才调用本地模型（例如通过 llama.cpp 加载的 GGUF 量化模型）辅助翻译。
- **低延迟优先** —— 捕获、OCR 与推理均不占用 UI 线程，配合缓冲复用、增量识别与结果缓存。

## 设计原则

1. **本地优先** —— 主链路任何时候都不需要网络访问。
2. **外置可扩展** —— 语料库、规则与插件都是普通文件，引擎本身无需改动。
3. **可解释** —— 每次翻译都会记录其来源（语料库 / 规则 / LLM）及置信度。
4. **线程隔离** —— OCR 与 LLM 推理永不阻塞 UI。

## 翻译主链路

```
热键 / 选区
  -> 屏幕捕获
  -> 图像预处理
  -> OCR
  -> 文本规范化
  -> 语料库匹配
  -> 规则推演
  -> （可选）本地 LLM
  -> 结果缓存
  -> 双语悬浮窗渲染
```

## 项目状态

**`prototype/` 下的 Python 原型已实现 M1–M3 并可实际运行**：屏幕区域捕获、本地 OCR、文件驱动的语料库与规则引擎、带术语保护的本地 NMT 模型，以及可点击穿透的字幕覆盖层。实测延迟数据与已知限制见 [`prototype/README.md`](prototype/README.md)。

**`src/` 下的 C++ 主干仍是骨架**，尚未承接上述成果，各层均为占位实现。

| 模块 | C++ 主干（`src/`） | Python 原型（`prototype/`） |
| --- | --- | --- |
| 屏幕捕获 / 选区 | 桩实现 —— 返回空白图像 | 可用（`mss`，支持拖拽选区） |
| OCR | 桩实现 —— 返回硬编码文本 | 可用（RapidOCR ONNX，离线） |
| 语料库加载 | 静态 JSON，每个 5 条 | 可用，分层 + 热加载 |
| 规则引擎 | 未实现 | 可用（词缀 / 词素 / 模板 / 音译） |
| 本地翻译模型 | 未实现（`src/llm/` 为空） | 可用（NLLB-200 int8，CTranslate2） |
| 术语保护（语料库权威翻译） | 未实现 | 可用，并报告兜底原因 |
| 存储 / 缓存 | 未实现 | 仅内存缓存 |
| UI、悬浮窗 | 未实现 | 可用（字幕条 + 对照面板） |
| 桌面控制窗口 | 未实现 | 可用（`--desktop`：六个页签，与悬浮窗共用同一 Tk root） |
| 插件 | 未实现；存在 Python 桥接原型 | 扩展点 v1，具备失败隔离，附带 2 个示例插件 |
| 界面架构 | 未实现 | 可用：引擎/界面事件契约边界，CLI、悬浮窗、桌面窗口与进程内 Web 面板共用同一 schema |
| 客制化（外观 / 版式 / 配置档） | 未实现 | 可用：声明式呈现规格、7 个预设、实时切换、内存档位 |
| 测试 | 仅有目录 | `--selftest` 加 8 个自检脚本，298 项断言 |

## 运行环境要求

Python 原型（现在即可运行）：

- Python 3.12；见 `prototype/requirements.txt`
- tkinter（标准库）用于覆盖层
- 可选：由 `prototype/fetch_model.py` 一次性获取的本地 NLLB 模型（约 617 MiB）。没有它时原型仅靠语料库与规则运行。

原生构建（尚不可用）：

- CMake >= 3.16 与支持 C++17 的编译器
- SQLite3、OpenCV（`CMakeLists.txt` 中为必需依赖）
- 可选：Qt 6（UI）、ONNX Runtime、llama.cpp、PaddleOCR

## 构建

原型无需构建，直接运行：

```bash
prototype\run.cmd --selftest
```

> **请用启动器，不要用裸 `python`。** 在很多 Windows 机器上，PATH 里的 `python`
> 是 Microsoft Store 的**应用执行别名**——一个 0 字节的 reparse point，运行它会弹出
> 「应用无法打开」或直接拉起商店，而不是执行 Python。`prototype\run.cmd`
>（Bash 下为 `prototype/run.sh`）会解析项目自带的解释器，因此文档里的命令
> 不受本机 `python` 含义的影响。启动器也会替你选择脚本：
>
> ```bash
> prototype\run.cmd --selftest            # 即 watashi_proto.py --selftest
> prototype\run.cmd fetch_model --check   # 即 fetch_model.py --check
> prototype\run.cmd bench_nmt --threads 4 # 即 bench_nmt.py --threads 4
> ```
>
> 想彻底消除那些弹窗，可在
> **设置 → 应用 → 高级应用设置 → 应用执行别名** 中关闭
> `python.exe` 与 `python3.exe`。

原生目标目前尚不能构建：

```bash
cmake -S . -B build
cmake --build build
```

> `CMakeLists.txt` 默认 `BUILD_TESTS=ON` 并调用 `add_subdirectory(tests)`，但 `tests/` 目录下目前还没有 `CMakeLists.txt`。在该文件补齐之前，请加上 `-DBUILD_TESTS=OFF` 进行配置。

依赖开关：

```bash
cmake -S . -B build -DUSE_QT=OFF -DUSE_ONNX=OFF -DUSE_PADDLEOCR=OFF -DBUILD_TESTS=OFF
```

## 运行

可用的原型：

```bash
prototype\run.cmd --select --mode both   # 拖拽选区后运行
prototype\run.cmd --list-monitors
prototype\run.cmd --mode none --duration 20 --print   # 无界面
```

原生目标（`./build/ProjectWatashi`）只会打印一行启动信息，尚未实现。

## 配置

配置项位于 `config/app.yaml`：

```yaml
translation:
  default_source: auto      # 源语言，或自动检测
  default_target: zh-CN     # 目标语言
  cache_enabled: true
  use_llm: true             # 本地 LLM，而非云端 API
  llm_backend: llama_cpp

ocr:
  provider: paddleocr
  capture_area_enabled: true

rules:
  slang_file: rules/slang/internet_slang.json
  fiction_file: rules/fiction/fantasy_terms.json

storage:
  db_path: data/project_watashi.db
```

## 自定义语料库格式

语料库文件为 JSON，以源词为键。按层级分目录：

| 层级 | 路径 | 优先级 |
| --- | --- | --- |
| 用户私有库 | `plugins/user/custom_rules/` | 最高 |
| 网络用语库 | `rules/slang/` | 中 |
| 虚构词汇库 | `rules/fiction/` | 中 |
| 通用词典 | `rules/dictionaries/` | 最低 |

示例（`rules/fiction/fantasy_terms.json`）：

```json
{
  "sword": "剑",
  "dragon": "龙",
  "spirit": "灵力",
  "void": "虚空",
  "realm": "境界"
}
```

词条可进一步扩展词性、领域标签、优先级、来源与更新时间等字段。查询顺序为：用户私有库 > 领域库 > 通用词典；同一层级内最长匹配优先。

词条文件改完即生效：引擎在每次翻译前检查修改时间并热加载（节流到每 500ms 一次，可用 `corpus.auto_reload` / `corpus.reload_interval_ms` 调整）。

**实时纠正**：译错了就地在屏幕上改。桌面端「字幕」页里点中那一行、填上正确译文即可，
记录写进用户私有库的 `corrections.json`，这一帧立刻改过来，之后同样的句子也用它。
整句粒度忽略首尾标点与空白（OCR 每次读出的标点都不同），词语粒度则该词在任何句子里都替换。

## 自定义规则

规则用于覆盖语料库中不存在的词汇：

| 类型 | 用途 |
| --- | --- |
| 词根 / 词缀拆分 | 组合已知词素 |
| 命名模板 | 套用某部作品的命名法 |
| 音译转写 | 按目标语言音系转写 |
| 领域映射 | 映射到既定术语译法 |
| 词素组合 | 将已知词拼接为新词 |

规则与语料库一样是外置文件。语料库命中的优先级始终高于规则推演，且规则结果可一键写入语料库。

## 插件

扩展点包括：翻译后处理、语料库加载器、规则集、OCR 前/后处理、悬浮窗渲染，以及导出格式。

- `plugins/builtin/` —— 随程序内置分发
- `plugins/user/` —— 用户自行安装

目前两套插件接口并存，尚未统一：

- C++ —— `include/plugins/plugin_api.h`（`IPlugin`，仍为草案）
- Python —— `prototype/watashi/plugins.py`（`PLUGIN_API_VERSION = 1`；**已可用**）

Python 侧当前状态：`postprocess` 与 `export` 两个扩展点已接通，
`corpus_loader` / `renderer` / `translator` 已预留但尚未接线。
版本不匹配、抛异常、返回类型错误、声明未知扩展点四类失败都会被捕获、记录并跳过，
不会拖垮主程序（`selfcheck_plugins` 的 35 项断言覆盖）。

插件需版本化、隔离并沙箱化：单个插件失败绝不能拖垮主程序，且默认禁止网络访问。

> **Python 侧做不到沙箱化，也不应假装做到**：插件与引擎同进程运行，Python 无法做权限降级
> 或网络拦截。因此当前定位是「给自己机器用的可信插件」，**不适合分发给他人**；
> 受限执行需要子进程加权限隔离，属于 C++ 主干的工作。

## 仓库结构

```
ProjectWatashi/
├── CMakeLists.txt
├── .gitignore       # 排除 .venv/ 与 models/（617 MiB 权重）
├── config/          # app.yaml 与用户偏好设置
├── docs/            # 架构、OCR 设计、插件 API、呈现规格
├── include/         # 公共头文件（app、core、db、llm、ocr、plugins）
├── src/
│   ├── app/         # 程序入口与装配
│   ├── core/        # 翻译路由（fiction_engine、network_slang 规划中）
│   ├── ocr/         # 捕获、预处理、OCR 调度
│   ├── db/          # 本地 SQLite 词库与缓存（规划中）
│   ├── llm/         # 本地 LLM 后端（规划中）
│   ├── rules/       # 规则引擎（规划中）
│   ├── ui/          # 悬浮窗与主控制台（规划中）
│   ├── plugins/     # 插件加载与隔离（规划中）
│   └── utils/       # 日志、字符串处理、加解密（规划中）
├── models/          # 本地模型（ocr、llm、embedding）—— 已 gitignore
├── plugins/         # 内置与用户插件
├── prototype/       # 可运行的 Python 原型：M1–M6
├── rules/           # 可编辑语料库（slang、fiction、dictionaries）
└── tests/           # C++ 测试；目前仅有 CMakeLists.txt，无测试源文件
```

原始脚手架里空的 `ui/` 与 `scripts/` 目录已删除：空目录无法提交，且没有任何构建引用它们；
它们对应的职责现由 C++ 树的 `src/ui/` 和当前可用的 `prototype/watashi/` 承担。
`src/bridge/`（旧的 Python 桥接层）与其加载的 `plugins/fiction_plugin.py`、
`plugins/slang_plugin.py` 也已删除——它们实现的是早期接口，与现行插件注册表不兼容且无任何引用。

## 路线图

| 阶段 | 目标 |
| --- | --- |
| M1 | 最小闭环：热键选区 -> 截图 -> OCR -> 语料库匹配 -> 双语悬浮窗（Windows）—— **已在 `prototype/` 完成** |
| M2 | 语料库与规则引擎落地，支持用户自定义与热加载 —— **已在 `prototype/` 完成** |
| M3 | 本地模型兜底翻译，含流式输出与缓存 —— **已在 `prototype/` 完成**（本地 NMT，以异步精修代替流式） |
| M4 | 达成延迟预算：增量识别、缓存、线程模型优化 —— **已部分实测** |
| M5 | 跨平台：Linux 与 macOS 的捕获层与 UI 适配 |
| M6 | 插件 API 稳定化；Android 目标实验性验证 —— **插件 API v1 已在 `prototype/` 完成**，Android 未开始 |

## 可移植性目标

| 平台 | 支持级别 | 关键依赖 |
| --- | --- | --- |
| Windows | 一级 | 桌面截屏 API、全局热键 |
| Linux | 一级 | X11 / Wayland 截屏、XTest |
| macOS | 一级 | ScreenCaptureKit / CGWindowList |
| Android | 二级 | MediaProjection、悬浮窗与无障碍权限 |
| 小型设备 | 三级 | 裁剪 UI 与模型体积，仅保留核心链路 |

## 说明

- 程序默认不进行任何云端调用，可离线工作。
- 全本地处理意味着文稿、术语库与翻译记录都留在用户自己的机器上。
- 此前的 README 描述的是一套与本仓库无关的 Tkinter + Google Translate 原型（`app.py`），该文件并不存在于本仓库中，现已由本文档替代。
