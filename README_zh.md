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
| 语料库加载 | 未实现 —— `src/` 里没有任何语料库相关代码 | 可用，分层 + 热加载 |
| 规则引擎 | 未实现 | 可用（词缀 / 词素 / 模板 / 音译） |
| 本地翻译模型 | 未实现（`src/llm/` 为空） | 可用（NLLB-200 int8，CTranslate2） |
| 术语保护（语料库权威翻译） | 未实现 | 可用，并报告兜底原因 |
| 存储 / 缓存 | 未实现 | 仅内存缓存 |
| UI、悬浮窗 | 未实现 | 可用（字幕条 + 对照面板） |
| 桌面控制窗口 | 未实现 | 可用（`--desktop`：字幕 / 采集 / 插件 三个页签，与悬浮窗共用同一 Tk root） |
| 插件 | 未实现；`include/plugins/plugin_api.h` 仍是草案 | 扩展点 v1，具备失败隔离，附带 2 个示例插件 |
| 界面架构 | 未实现 | 可用：引擎/界面事件契约边界，CLI、悬浮窗、桌面窗口与进程内 Web 面板共用同一 schema |
| 客制化（外观 / 版式 / 配置档） | 未实现 | 可用：声明式呈现规格、7 个预设、实时切换、内存档位 |
| 测试 | 仅有目录 | `--selftest` 加 20 个自检脚本（15 个无需界面、CI 可跑，5 个需要显示器）；具体断言数记录在 `prototype/README.md` |

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

没有浏览器、甚至没有界面的机器，也可以在命令行上管理词库：

```bash
prototype\run.cmd --list-scenes                       # 场景、当前场景、全部冲突
prototype\run.cmd --export-corpus corpus.json          # 改完再写回去
prototype\run.cmd --import-corpus terms.csv --dry-run  # 只报告，不写入
prototype\run.cmd --promote-corrections --dry-run      # 实时纠正 -> 语料库词条
```

原生目标（`./build/ProjectWatashi`）只会打印一行启动信息，尚未实现。

## 配置

可运行的原型读的是 `prototype/config.yaml`，文件里的路径都相对该文件解析，所以从任何目录启动都可以。在界面里改的设置**不会**写回这个文件：它们写进 `prototype/config.user.yaml`，一个在加载时合并上来的覆盖层。这样安排是有意的——`config.yaml` 里的注释本身就是这些配置项的说明，用 YAML 库往返写一遍会把它们全删掉；而且「还原」因此就是删掉一个文件。`--config PATH` 可以让原型去读另一个文件。

坏消息是布局：`config/app.yaml` 还留在仓库里，它是给 C++ 主干预留的占位文件，**目前没有任何代码读它**。真正的配置项清单在 `prototype/watashi/settings_schema.py`——一份声明同时给出键名、标签与说明，桌面设置表单和 Web 面板渲染的就是它；出厂默认值在 `prototype/watashi/config.py` 的 `DEFAULTS` 里。配置文件里写了引擎不认识的键会**被静默忽略**，所以一个拼错的键看起来和「这个设置不生效」完全一样；要看真实存在哪些设置，以设置面板为准。

一份最小的配置，形状与加载器一致（下面每个键和取值都是真的）：

```yaml
capture:
  region: null          # "x,y,w,h"（物理像素）；null = 屏幕下方一条横带
  fps: 10               # 采集频率；画面没变就不会识别
  settle_ms: 0          # 画面静止这么久之后才识别

translation:
  source: auto          # 自动判断，或直接写明：自动判断分不出法语和英语
  target: zh-CN
  scene: ""             # 哪个场景的词条优先；留空 = 不偏袒
  nmt_model: null       # null = 只用语料库与规则；出厂文件里写的是模型路径

corpus:
  domain: [corpus, ../rules/slang, ../rules/fiction]
  auto_reload: true     # 词条文件修改时间变了就重新加载

overlay:
  mode: both            # bar | panel | both | none
```

出厂的 `config.yaml` 比这长得多，值得读而不是照着抄：它的注释写了每个默认值背后的实测理由（为什么 `ocr.det_limit_type` 是 `max` 而不是 RapidOCR 的 `min`、为什么 `max_width` 保持 `0`）。

## 自定义语料库格式

语料库文件为 JSON。一条词条由**（目标语言、源词、场景、条件）**共同决定 —— 这四件事决定了眼前这一行该用哪个答案。按层级分目录：

| 层级 | 路径 | 优先级 |
| --- | --- | --- |
| 用户私有库 | `plugins/user/custom_rules/` | 最高 |
| 网络用语库 | `rules/slang/` | 中（领域层） |
| 虚构词汇库 | `rules/fiction/` | 中（领域层） |
| 通用词典 | `rules/dictionaries/` | 最低 |

示例 —— `rules/fiction/fantasy_terms.json` 的出厂内容：

```json
{
  "lang": "zh-CN",
  "entries": {
    "sword": "剑",
    "dragon": "龙",
    "spirit": "灵力",
    "void": "虚空",
    "realm": "境界"
  }
}
```

词条还可以带 `pos`、`priority`、`note`：`pos` 与 `note` 会一路带到界面上显示，但不参与任何判定；`priority` 在同层、同场景、同条件下用来打破平手。查询顺序为：用户私有库 > 领域库 > 通用词典；同一层级内最长匹配优先。

**语料库不是语言中立的**：词条要声明自己译文的语言（文件级 `"lang": "zh-CN"` 配 `"entries"` 对象，或每条词自己写 `lang`），否则一份英→中词表在被要求译成日语时会**满置信度地给出中文**。不声明即视为"任何目标语言都可用"，既有文件因此不受影响；同一个源词可以按语言、场景与条件各存一条。语言按语言比较（`zh` = `zh-CN` = `zho_Hans`），语料库能覆盖哪些语言会出现在 ready 事件、桌面窗口与 Web 面板里。

### 一个词的多种意思

写第二条意思曾经会**静默覆盖**第一条 —— 两条落在同一个键上，而且没有任何地方会告诉你。现在这个键把场景和条件也算进去；而一个 JSON 对象装不下同名的两个键，所以一个源词有多个答案时写成**列表**：

```json
{
  "bank": [
    {"target": "银行", "domain": "finance"},
    {"target": "岸", "domain": "geography"}
  ]
}
```

区分两种意思有两条路。

**场景**（词条上的 `domain`）由你来选：在设置面板里改 `translation.scene`，在桌面顶栏「目标语言」旁边的「场景」框里填，或用 `--scene NAME`。留空（默认）表示不偏袒，也就是场景出现之前的行为。场景名按大小写折叠比较，`Finance` 与 `finance` 是同一个场景。

**条件**是引擎自己在画面里找到的证据，写了几条就必须**全部**成立：

| 键 | 成立条件 |
| --- | --- |
| `when_line` | 该正则在整行识别文本里能匹配到 |
| `when_near` | 这些词中有任意一个出现在该行**被匹配词之外**的地方 |
| `when_window` | 该正则在捕获窗口的标题里能匹配到 |

`when_near` 看的是把被匹配词挖掉之后的整行，所以给 "bank" 写的 `when_near: ["bank"]` 不会因为它自己而成立。要求窗口的条件在**没有窗口时不成立** —— 固定区域截屏没有标题可匹配 —— 这正是窗口专用词条不会泄漏到区域捕获里的原因。

多个词条都能回答时，顺序是：

1. **层级优先** —— 你自己写的词条始终压过出厂词条，哪怕出厂那条标了你正在用的场景；
2. **场景** —— 精确匹配 > 没标场景的 > 标了别的场景的；
3. **`priority`**，之后是确定的字典序收尾，答案不会取决于哪个文件先被读到。

条件是**过滤器而不是权重**：它决定一条词条是否有资格参与；没通过条件的词条不是排在后面，而是根本不参与竞争。

**两条真正相同的词条** —— 同源词、同语言、同场景、同条件 —— 却给出**不同译文**时，赢家依旧由规则确定，但输家现在会被报告出来，而不是静默丢弃：加载语料库时打印，`--list-scenes` 里列出，Web 面板的「语料库」页里连同原因一起显示。译文相同的两条不会被报告：那只是同一句话写了两遍，而一个乱报警的诊断没人会看。要消掉一条被报告的冲突，办法是给两条不同的场景或不同的条件 —— 这是唯一能说明「什么时候该用哪条」的表达方式。

词条文件改完即生效：引擎在每次翻译前检查修改时间并热加载（节流到每 500ms 一次，可用 `corpus.auto_reload` / `corpus.reload_interval_ms` 调整）。

**缓存按情形分键，而不是按文本分键。**最近翻译记忆（10 秒内见过的句子不再重新翻译）过去只按源文本分键，于是在这 10 秒里切换目标语言，会把上一种语言的译文原样送回来；现在它按语料库真正能区分的维度分键 —— 始终带目标语言，只有当确实有词条标了场景、或写了窗口条件时，才把场景 / 窗口算进去。所以既没有场景也没有窗口条件的语料库，缓存行为和以前完全一样；而能按场景给出不同答案的语料库，也不会把另一个场景的答案递回来。翻译缓存与模型精修缓存用的是同一个键。

**实时纠正**：译错了就地在屏幕上改。桌面端「字幕」页里点中那一行、填上正确译文即可，
记录写进用户私有库的 `corrections.json`，这一帧立刻改过来，之后同样的句子也用它。
整句粒度忽略首尾标点与空白（OCR 每次读出的标点都不同），词语粒度则该词在任何句子里都替换。
每条纠正还会记下它是为哪种目标语言写下的：过去在译成中文时写下的整句纠正，切到日语之后
照样生效，于是你读到的是一段标着日语的中文。没有标语言的纠正 —— 也就是这个字段出现之前
写下的所有文件 —— 仍然对任何目标语言生效，引擎把它们单独统计为**未标注**，而不是替你猜一个
语言。引擎命令（`correct`、`list_corrections`、`remove_correction`）见 `prototype/README.md`。

**在界面里编辑语料库**：Web 面板的「语料库」页就是编辑器 —— 词条表格、覆盖、停用、还原、
导入导出，每行带自己的场景，以及加载器发现的冲突。**改出厂词条不会改写出厂文件**：它以
用户层的一条同名覆盖生效，界面会告诉你它覆盖了哪条、原来是什么、可一键还原。导入可以选文件，
也可以**直接粘贴文本**；导出支持 JSON（语料库自身的写法）、CSV/TSV（表头可中文，Excel 直接打开），
其它格式走插件的 `corpus_loader` 扩展点。所有改动立即生效，不需要重启。

一个源词有多个答案时 —— 两种语言，或同一语言的两种场景 —— 存的是**列表**，编辑器现在既会写
这种形式也会读它。它以前只写不读：这样的词在表格里显示为空，下一次保存就把空表写回文件，
把两条词条一起抹掉。现在读取、写入、导入、导出四条路径都认这种形式。

**批量录入**：原本已有三条路 —— 面板的文件导入、把文件丢进用户语料库层
（`plugins/user/custom_rules/`），以及插件的 `corpus_loader` 扩展点。现在又多了四条：

- 面板可以**直接粘贴文本**，不必先有文件 —— 一行一条 `原文,译文`，也可以带表头多列；
- 面板可以在写入前**预览**导入：「预览」和「导入」调的是同一个接口，只多一个 `dry_run`，
  所以预览给出的「新增 / 更新 / 跳过」就是真导入会产生的结果，而不是另算一遍的猜测；
- 记录下来的纠正可以**批量提升为词条** —— 面板「语料库」页「实时纠正」栏里的「提升为词条」，或
  `--promote-corrections`。纠正文件与语料库仍然是两个独立文件；不问就不会提升，提升也不会
  改写或删除任何一条纠正；
- **命令行**可以完全无界面地做这些事：

  | 命令 | 作用 |
  | --- | --- |
  | `--import-corpus FILE` | 从文件批量新增词条 |
  | `--export-corpus FILE` | 把语料库写出成文件 |
  | `--promote-corrections` | 把记录下来的纠正复制成词条 |
  | `--list-scenes` | 列出语料库声明的场景、当前场景与全部冲突 |
  | `--scene NAME` | 选择当前场景（留空即清除） |
  | `--corpus-format json\|csv\|tsv` | 导入 / 导出的格式；默认按扩展名，否则 json |
  | `--corpus-scope user\|effective` | 导出你自己的词条或全部生效词条；配 `--promote-corrections` 时改为 `line` / `term` |
  | `--keep-existing` | 导入 / 提升时跳过已存在的条目，而不是覆盖 |
  | `--dry-run` | 导入 / 提升：只报告会发生什么，什么都不写 |

CSV/TSV 导入导出认这些列：`source,target,lang,pos,domain,when_line,when_near,when_window,note`，
表头也接受中文 —— 准确清单见 `prototype/watashi/library.py` 的 `_COLUMNS`。一个源词有多个答案时，
导出的就是上面那种列表形式：一个 JSON 对象装不下第二个答案，不用列表就等于静默丢掉一半。

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

扩展点包括：翻译后处理、语料库加载器、导出格式、悬浮窗渲染，以及替换整个翻译后端 ——
对应 `prototype/watashi/plugins.py` 里的 `postprocess`、`corpus_loader`、`export`、
`renderer`、`translator`。

- `plugins/builtin/` —— 随程序内置分发
- `plugins/user/` —— 用户自行安装

目前两套插件接口并存，尚未统一：

- C++ —— `include/plugins/plugin_api.h`（`IPlugin`，仍为草案）
- Python —— `prototype/watashi/plugins.py`（`PLUGIN_API_VERSION = 1`；**已可用**）

Python 侧当前状态：`postprocess`、`export` 与 `corpus_loader` 三个扩展点已接通
—— `corpus_loader` 就是让插件读内置 JSON 之外的语料库格式的那个 ——
`renderer` / `translator` 已预留但尚未接线；`--list-plugins` 会告诉你哪些已接通，
作者不必靠「插件被无视了」才发现。
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
├── config/          # app.yaml —— C++ 主干的占位文件，目前没有代码读它
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
├── prototype/       # 可运行的 Python 原型：M1–M3 加插件 API v1
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
