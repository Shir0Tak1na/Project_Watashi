"""The settings schema: one declaration that every surface renders.

Why a schema rather than labels written into each UI: the desktop window, the web
panel, the CLI and the docs would otherwise each describe the same setting in their
own words, and they drift. Every one of these fields carries a Chinese label and a
plain-language description, and the surfaces render whatever is here.

Three properties matter more than the descriptions themselves:

* **``applies``** -- whether a change takes effect immediately or only after a
  restart. The OCR thread count, the detector parameters, the model settings and
  plugin discovery are all read while the engine is being constructed, so a UI that
  offers them as live controls is lying to the user: they change the value, nothing
  happens, and they conclude the program is broken. Every such field says so.
* **``cost``** -- the honest trade-off. A setting that looks free but costs 1-4 s per
  frame on a dense screen must say that where the user is looking, not only in a
  README.
* **``danger``** -- settings that are a measured trap rather than a matter of taste.
  ``max_width`` is the example: downscaling looks like an obvious optimisation and
  measured *slower* and less accurate.

`selfcheck_settings` asserts this schema stays in step with the real config, in both
directions, because a settings UI that drifts from reality is worse than none.
"""

from __future__ import annotations

from dataclasses import dataclass, field as dc_field
from typing import Any, Iterable

LIVE = "live"
RESTART = "restart"


@dataclass(frozen=True)
class Field:
    """One adjustable setting, described for a human."""

    key: str
    label: str
    description: str
    kind: str  # bool | int | float | enum | text | path | path_list | region
    default: Any = None
    choices: tuple[str, ...] = ()
    low: float | None = None
    high: float | None = None
    unit: str = ""
    applies: str = LIVE
    cost: str = ""
    danger: str = ""
    note: str = ""
    #: dotted key of a command that applies this live, when one exists
    command: str = ""

    def to_dict(self) -> dict[str, Any]:
        data = {
            "key": self.key,
            "label": self.label,
            "description": self.description,
            "kind": self.kind,
            "default": self.default,
            "applies": self.applies,
        }
        for name in ("choices", "low", "high", "unit", "cost", "danger", "note", "command"):
            value = getattr(self, name)
            if value not in ("", (), None):
                data[name] = value
        return data


@dataclass(frozen=True)
class Category:
    id: str
    title: str
    summary: str
    fields: tuple[Field, ...] = dc_field(default_factory=tuple)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "summary": self.summary,
            "fields": [f.to_dict() for f in self.fields],
        }


# --------------------------------------------------------------------------- #
# ① 采集：看哪里
# --------------------------------------------------------------------------- #

CAPTURE = Category(
    id="capture",
    title="① 采集 · 看哪里",
    summary="决定识别屏幕的哪一块、多久看一次、画面变化多大才算变化。"
            "这一组直接影响延迟与耗电，建议先调这里。",
    fields=(
        Field(
            key="capture.region",
            label="识别区域",
            description="要识别的屏幕矩形，格式 x,y,w,h（物理像素）。留空则自动取"
                        "屏幕下方一条横带——字幕通常在那里。区域越小孩子越快："
                        "字幕条约 90ms/帧，整窗密文本要 1–4 秒/帧。",
            kind="region",
            default=None,
            command="set_region",
            note="推荐：拖拽选区（桌面窗口的「框选区域…」按钮，或 Ctrl+Alt+R）",
        ),
        Field(
            key="capture.monitor",
            label="显示器",
            description="用哪个显示器。0 表示所有显示器合并成一块虚拟屏幕。",
            kind="int",
            default=1,
            low=0,
            high=8,
            applies=RESTART,
            note="仅在「识别区域」留空（自动横带）时生效",
        ),
        Field(
            key="capture.region_ratio",
            label="自动横带高度比例",
            description="自动取区域时，横带占屏幕高度的比例。0.18 即屏幕最下方 18%。",
            kind="float",
            default=0.18,
            low=0.05,
            high=0.6,
            applies=RESTART,
        ),
        Field(
            key="capture.fps",
            label="采集帧率",
            description="每秒截屏并比对的次数。它不等于识别帧率——画面没变就不会识别。"
                        "调高只增加一点比对开销，调低可能漏掉一闪而过的字幕。",
            kind="float",
            default=10.0,
            low=1.0,
            high=60.0,
            unit="帧/秒",
            command="set_fps",
        ),
        Field(
            key="capture.diff_threshold",
            label="变化阈值",
            description="画面变化多大才算「变了」并触发识别。数值越小越敏感，短字幕"
                        "不易漏；越大越省 CPU，但可能漏掉一闪而过的字幕。",
            kind="float",
            default=2.0,
            low=0.0,
            high=255.0,
            command="set_diff_threshold",
            note="参考：字幕约 2，密集变化的界面约 4",
        ),
        Field(
            key="capture.settle_ms",
            label="稳定等待（毫秒）",
            description="画面停止变化多久之后才做识别。0 = 一变就识别，也就是在文字"
                        "还在移动的中途截图，对滚动文字和弹幕会得到乱码。"
                        "设成 150–250 可以只在文字停下来时识别，准确率和速度都更好。",
            kind="int",
            default=0,
            low=0,
            high=2000,
            unit="毫秒",
            applies=RESTART,
            cost="代价：静止字幕首次出现会晚 settle_ms 才显示",
            note="这是「实时 ↔ 准确」的总开关，弹幕场景建议 150–250",
        ),
        Field(
            key="capture.min_ocr_interval",
            label="识别最小间隔",
            description="两次识别之间至少间隔多少秒，用来给 CPU 设一个上限。"
                        "0 = 不限。",
            kind="float",
            default=0.0,
            low=0.0,
            high=10.0,
            unit="秒",
            applies=RESTART,
        ),
        Field(
            key="capture.signature_width",
            label="比对指纹宽度",
            description="用来比较两帧是否相同的小图宽度（像素）。越大越不容易漏掉"
                        "小范围变化，但比对本身更慢。",
            kind="int",
            default=96,
            low=32,
            high=480,
            unit="像素",
            applies=RESTART,
        ),
        Field(
            key="capture.max_width",
            label="识别前缩放宽度",
            description="识别前把画面缩到多宽。0 = 原始分辨率。",
            kind="int",
            default=0,
            low=0,
            high=4096,
            unit="像素",
            applies=RESTART,
            danger="实测陷阱：降采样并不会稳定地省时间，而且会掉准确率"
                   "（1600 宽原始 354ms，缩到 960 反而 246ms，缩到 640 时"
                   "「he broke through」被识别成「hebrokethroughtothevoidrealm」）。"
                   "真正的提速手段是缩小识别区域，不是缩放像素。",
            note="建议保持 0",
        ),
    ),
)

# --------------------------------------------------------------------------- #
# ② 识别：OCR 引擎
# --------------------------------------------------------------------------- #

OCR = Category(
    id="ocr",
    title="② 识别 · OCR 引擎",
    summary="RapidOCR / ONNX Runtime 的底层参数。这几项在引擎启动时读取，"
            "改完必须重启，所以放在说明里而不是放在常用位置。",
    fields=(
        Field(
            key="ocr.intra_op_threads",
            label="推理线程数",
            description="ONNX Runtime 做识别时用几个线程。默认会让它用满所有核心，"
                        "但在小模型上线程互相抢内存带宽反而更慢。",
            kind="int",
            default=4,
            low=1,
            high=32,
            unit="线程",
            applies=RESTART,
            note="实测最优：640 宽画面下 1→133ms，2→80ms，3→61ms，4→168ms，"
                 "6→261ms，8→384ms。这台机器的甜点是 3–4",
        ),
        Field(
            key="ocr.inter_op_threads",
            label="并行算子线程数",
            description="并行执行不同算子的线程数。识别是单帧串行的，调高没有收益。",
            kind="int",
            default=1,
            low=1,
            high=8,
            applies=RESTART,
            note="建议保持 1",
        ),
        Field(
            key="ocr.use_mem_arena",
            label="内存池复用",
            description="复用推理所需的显存/内存缓冲区，而不是每次重新分配。"
                        "关掉会让每帧都产生分配开销。",
            kind="bool",
            default=True,
            applies=RESTART,
            note="建议保持开启",
        ),
        Field(
            key="ocr.det_limit_type",
            label="检测缩放策略",
            description="RapidOCR 默认按「短边」放大到 736 像素，会把一条 1280×176 的"
                        "字幕带放大成 5344×736（17 倍像素），仅检测就要约 2300ms。"
                        "改成 max 之后只会缩小、不会放大。",
            kind="enum",
            default="max",
            choices=("max", "min"),
            applies=RESTART,
            danger="实测陷阱：保持 min（RapidOCR 默认）会让识别慢约 50 倍",
            note="建议保持 max",
        ),
        Field(
            key="ocr.det_limit_side_len",
            label="检测缩放边长",
            description="配合上面的缩放策略使用，指定目标边长。留空 = 用 RapidOCR"
                        "内置值（736）。通常不需要改。",
            kind="int",
            default=None,
            low=64,
            high=4096,
            unit="像素",
            applies=RESTART,
            note="建议留空",
        ),
        Field(
            key="ocr.use_cls",
            label="方向分类",
            description="判断文字是否颠倒后再识别。字幕都是横排正立的，这个步骤"
                        "纯属额外开销。",
            kind="bool",
            default=False,
            applies=RESTART,
            note="如果识别竖排或倒置文字才需要打开",
        ),
    ),
)

# --------------------------------------------------------------------------- #
# ③ 翻译
# --------------------------------------------------------------------------- #

TRANSLATION = Category(
    id="translation",
    title="③ 翻译 · 语言与语料库优先",
    summary="翻译成什么语言、源语言是什么，以及语料库在多大程度上压过模型。",
    fields=(
        Field(
            key="translation.target",
            label="目标语言",
            description="要翻译成什么语言。屏幕上已经是该语言的行会被直接放行、"
                        "不送翻译，所以设对了能省下大量模型开销。",
            kind="text",
            default="zh-CN",
            command="set_target_lang",
            note="可用简称：zh-CN、zh-TW、en、ja、ko、fr、de、es、pt、it、ru、ar、"
                 "th、vi、id；其他语言可直接写 NLLB 代码，如 nld_Latn（荷兰语）",
        ),
        Field(
            key="translation.source",
            label="源语言",
            description="屏幕文字是什么语言。auto = 自动判断。自动判断靠字形，"
                        "对汉字/假名/谚文/俄文/阿拉伯文可靠，但分不出法语和英语——"
                        "所以只要源语言是英语以外的拉丁字母语言，就必须在这里指定。",
            kind="text",
            default="auto",
            applies=RESTART,
            danger="不指定时，法语、德语、越南语等会被当成英语：若目标是英语会被"
                   "原样放行（不翻译），若目标是中文则会把英语当源语言、输出完全跑偏",
            note="日语含假名时可自动判断；纯汉字标题会被误判成中文，建议直接指定 ja",
        ),
        Field(
            key="translation.min_confidence",
            label="最低置信度",
            description="低于此平均置信度的识别结果直接丢弃。0 = 全部保留。",
            kind="float",
            default=0.0,
            low=0.0,
            high=1.0,
            applies=RESTART,
        ),
        Field(
            key="translation.protect_terms",
            label="术语保护",
            description="把语料库命中的词先用占位符遮住再送模型，翻译完成后还原。"
                        "这是保证「虚空剑」不会每次被译成不同东西的关键机制。",
            kind="bool",
            default=True,
            applies=RESTART,
            cost="代价：遮住术语会减少上下文，句子通顺度会略降",
            note="建议保持开启",
        ),
        Field(
            key="translation.protect_min_confidence",
            label="术语保护的置信度门槛",
            description="只有置信度不低于此值的命中才当作权威并遮罩。设得太低会让"
                        "音译兜底（置信度 0.10）遮住整句话。",
            kind="float",
            default=0.4,
            low=0.0,
            high=1.0,
            applies=RESTART,
            note="0.4 是实测值：低于它会整句被遮住",
        ),
    ),
)

# --------------------------------------------------------------------------- #
# ④ 本地模型
# --------------------------------------------------------------------------- #

MODEL = Category(
    id="model",
    title="④ 本地模型 · 句子通顺度",
    summary="本地的 NLLB-200 翻译模型。语料库负责术语正确，模型只负责补足语法。"
            "不装模型程序照样能跑，只用语料库与规则。",
    fields=(
        Field(
            key="translation.nmt_model",
            label="模型目录",
            description="本地 CTranslate2 模型的目录路径。留空 = 不加载模型，"
                        "只用语料库与规则（内存从约 890MiB 降到 175MiB）。",
            kind="path",
            default=None,
            applies=RESTART,
            note="用 run.cmd fetch_model 一次性下载（约 617 MiB）",
        ),
        Field(
            key="translation.nmt_compute_type",
            label="计算精度",
            description="模型权重的数值精度。int8 最快、体积最小；float32 最准但慢。",
            kind="enum",
            default="int8",
            choices=("int8", "int8_float32", "float32"),
            applies=RESTART,
            note="int8 实测够用",
        ),
        Field(
            key="translation.nmt_beam_size",
            label="束搜索宽度",
            description="生成时同时保留几个候选。宽度越大越慢。",
            kind="int",
            default=1,
            low=1,
            high=8,
            applies=RESTART,
            danger="实测反直觉：beam 4 比 beam 1 更慢而且输出更差",
            note="建议保持 1",
        ),
        Field(
            key="translation.nmt_intra_threads",
            label="模型线程数",
            description="模型推理用几个线程。和 OCR 一样，小模型上线程过多会更慢。",
            kind="int",
            default=4,
            low=1,
            high=32,
            unit="线程",
            applies=RESTART,
            cost="模型与 OCR 会争抢内存带宽：并发时 OCR 从 73ms 恶化到 266ms，"
                 "所以模型只在 OCR 空闲时才精修",
            note="实测最优 4",
        ),
    ),
)

# --------------------------------------------------------------------------- #
# ⑤ 语料库与规则
# --------------------------------------------------------------------------- #

CORPUS = Category(
    id="corpus",
    title="⑤ 语料库与规则 · 让生词可译",
    summary="这是本项目区别于通用翻译工具的地方。词条是磁盘上的普通文件，"
            "改完按修改时间自动热加载，不需要重启。",
    fields=(
        Field(
            key="corpus.user",
            label="用户私有库目录",
            description="你自己的术语表放在这里，优先级最高，会压过其他所有层。",
            kind="path_list",
            default=["../plugins/user/custom_rules"],
            applies=RESTART,
            note="目录内所有 .json 都会被加载",
        ),
        Field(
            key="corpus.domain",
            label="领域库目录",
            description="按题材划分的词库：网络用语、虚构词汇等，优先级低于用户库。",
            kind="path_list",
            default=["corpus", "../rules/slang", "../rules/fiction"],
            applies=RESTART,
        ),
        Field(
            key="corpus.general",
            label="通用词典目录",
            description="基础高频词的目录，优先级最低，只在上面两层都没命中时才查。"
                        "放通用词汇，不要放作品专属术语。",
            kind="path_list",
            default=["../rules/dictionaries"],
            applies=RESTART,
        ),
        Field(
            key="rules.files",
            label="规则文件",
            description="语料库没命中时的推演规则：词根词缀拆分、构词模板、"
                        "音译兜底。改完热加载即可生效。",
            kind="path_list",
            default=["rules/engine_rules.json"],
            applies=RESTART,
            note="当前有 8 条规则：affix/morpheme/template/transliterate 四类",
        ),
    ),
)

# --------------------------------------------------------------------------- #
# ⑥ 呈现
# --------------------------------------------------------------------------- #

PRESENTATION = Category(
    id="presentation",
    title="⑥ 呈现 · 字幕长什么样",
    summary="外观与版式，立即生效，桌面窗口与悬浮窗共用同一份描述。",
    fields=(
        Field(
            key="overlay.mode",
            label="显示方式",
            description="bar = 屏幕下方的字幕条（点击穿透）；panel = 可拖动的"
                        "双语对照面板（可交互）；both = 两者都要；none = 什么都不画，"
                        "只用于测量延迟。",
            kind="enum",
            default="both",
            choices=("bar", "panel", "both", "none"),
            applies=RESTART,
        ),
        Field(
            key="presentation",
            label="呈现预设",
            description="字幕的整体外观方案。bar = 半透明底板 + 原文小字 + 大号译文；"
                        "bare = 完全透明 + 描边文字（真字幕观感）；inplace = 把译文画在"
                        "原文位置上并盖住原文；lines = 每行一个双语块；"
                        "panel = 双语历史；hidden = 只识别不显示。",
            kind="enum",
            default="bar",
            choices=("bar", "bare", "minimal", "lines", "inplace", "panel", "hidden"),
            command="set_presentation",
        ),
        Field(
            key="overlay.subtitle_style",
            label="字幕底色",
            description="plate = 半透明深色底板，稳；bare = 真透明 + 描边，观感更像"
                        "压制字幕，但某些显卡上字边缘会有杂色。",
            kind="enum",
            default="plate",
            choices=("plate", "bare"),
            applies=RESTART,
        ),
        Field(
            key="overlay.font_family",
            label="字体",
            description="留空 = 自动挑一个能显示中文的字体。",
            kind="text",
            default=None,
            applies=RESTART,
        ),
        Field(
            key="overlay.subtitle_size",
            label="译文字号",
            description="悬浮字幕里译文的大小。原文那一行按它的 0.67 倍自动推算。"
                        "调大更容易看清，但会同时撑高整个字幕块，占用更多屏幕。",
            kind="int",
            default=24,
            low=8,
            high=96,
            unit="磅",
            applies=RESTART,
        ),
        Field(
            key="overlay.bar_height",
            label="字幕条高度",
            description="字幕条窗口的高度。文字本身会撑开所需空间，这个值是容器高度。",
            kind="int",
            default=96,
            low=32,
            high=600,
            unit="像素",
            applies=RESTART,
        ),
        Field(
            key="overlay.bar_alpha",
            label="底板不透明度",
            description="字幕条底板的透明度。0 = 全透明（只剩文字），1 = 不透明。",
            kind="float",
            default=0.72,
            low=0.0,
            high=1.0,
            applies=RESTART,
        ),
        Field(
            key="overlay.bar_bottom_margin",
            label="距屏幕底部",
            description="字幕条离屏幕下边缘多少像素。调大可以让字幕避开播放器自带的"
                        "控制条和进度条，调小则更贴近底边。",
            kind="int",
            default=90,
            low=0,
            high=1000,
            unit="像素",
            applies=RESTART,
        ),
        Field(
            key="overlay.panel_width",
            label="面板宽度",
            description="双语对照面板的宽度。也可以在面板右下角直接拖动缩放，"
                        "拖过的尺寸会覆盖这个值。",
            kind="int",
            default=540,
            low=240,
            high=2560,
            unit="像素",
            applies=RESTART,
        ),
        Field(
            key="overlay.panel_height",
            label="面板高度",
            description="双语对照面板的高度。同样可以拖动缩放。",
            kind="int",
            default=320,
            low=140,
            high=1600,
            unit="像素",
            applies=RESTART,
        ),
        Field(
            key="overlay.panel_size",
            label="面板字号",
            description="双语对照面板正文的字号。面板标题和状态行会在这基础上自动增减，"
                        "所以改这一项就够，不用分别调。",
            kind="int",
            default=13,
            low=8,
            high=40,
            unit="磅",
            applies=RESTART,
        ),
        Field(
            key="overlay.panel_history",
            label="面板保留条数",
            description="面板里最多显示多少条对照记录。",
            kind="int",
            default=12,
            low=1,
            high=200,
            unit="条",
            applies=RESTART,
        ),
        Field(
            key="overlay.dim_low_confidence",
            label="低置信度变暗",
            description="识别置信度低的行显示得淡一些，方便一眼看出哪些不太可靠。",
            kind="bool",
            default=True,
            applies=RESTART,
            danger="当前无效：这个开关与呈现规格里的元素透明度一样，"
                   "被绘制层忽略了——文字始终按 100% 不透明度绘制",
        ),
    ),
)

# --------------------------------------------------------------------------- #
# ⑦ 性能
# --------------------------------------------------------------------------- #

PERFORMANCE = Category(
    id="performance",
    title="⑦ 性能与内存 · 按机器档位",
    summary="一组预设参数，决定加载哪些组件。改完需要重启。",
    fields=(
        Field(
            key="profile",
            label="配置档",
            description="lean = 不加载模型，约 175 MiB，只用语料库与规则，延迟最低；"
                        "balanced = 常规桌面用法，约 890 MiB；full = 全部开启。",
            kind="enum",
            default=None,
            choices=("lean", "balanced", "full"),
            command="load_profile",
            cost="balanced/full 会多占用 715 MiB（模型权重本身）",
        ),
    ),
)

# --------------------------------------------------------------------------- #
# ⑧ 插件
# --------------------------------------------------------------------------- #

PLUGINS = Category(
    id="plugins",
    title="⑧ 插件 · 用你自己的代码扩展",
    summary="扩展点目前接通了「翻译后处理」与「导出格式」两项。"
            "插件与引擎同进程运行，Python 无法沙箱化。",
    fields=(
        Field(
            key="plugins.enabled",
            label="启用插件",
            description="关闭后完全不扫描插件目录，启动更快，也不会有插件影响翻译结果。",
            kind="bool",
            default=True,
            applies=RESTART,
        ),
        Field(
            key="plugins.directories",
            label="插件目录",
            description="额外的插件目录。留空 = 内置目录 plugins/builtin 加上"
                        "用户目录 plugins/user。",
            kind="path_list",
            default=[],
            applies=RESTART,
        ),
        Field(
            key="plugins.postprocess",
            label="启用翻译后处理",
            description="让插件的后处理链有机会修改最终译文（术语统一、清理 OCR 杂字）。",
            kind="bool",
            default=True,
            applies=RESTART,
        ),
        Field(
            key="plugins.history_limit",
            label="导出历史条数",
            description="为导出功能保留多少条已识别记录。有上限是因为常驻悬浮窗"
                        "会无限增长。",
            kind="int",
            default=2000,
            low=10,
            high=100000,
            unit="条",
            applies=RESTART,
        ),
    ),
)

# --------------------------------------------------------------------------- #
# ⑨ 诊断
# --------------------------------------------------------------------------- #

DIAGNOSTICS = Category(
    id="diagnostics",
    title="⑨ 诊断与日志 · 看清楚发生了什么",
    summary="排查问题时打开。会往终端输出内容，平时保持关闭以免干扰。",
    fields=(
        Field(
            key="logging.print_lines",
            label="输出识别结果",
            description="把每一条识别到的原文与译文打到终端。",
            kind="bool",
            default=False,
            applies=RESTART,
        ),
        Field(
            key="logging.print_trace",
            label="输出来源追踪",
            description="额外打印每个片段命中了哪条语料库词条或哪条规则、置信度多少。"
                        "用来判断某个词为什么这样翻译。",
            kind="bool",
            default=False,
            applies=RESTART,
        ),
        Field(
            key="logging.print_refined",
            label="输出模型精修结果",
            description="模型把句子改写完后，把结果打到终端。",
            kind="bool",
            default=True,
            applies=RESTART,
        ),
    ),
)


CATEGORIES: tuple[Category, ...] = (
    CAPTURE,
    OCR,
    TRANSLATION,
    MODEL,
    CORPUS,
    PRESENTATION,
    PERFORMANCE,
    PLUGINS,
    DIAGNOSTICS,
)

#: Config keys that deliberately have no user-facing field. Empty today: every key
#: in the shipped config is described above. `selfcheck_settings` fails when a config
#: key appears in neither this set nor the schema, because an undocumented setting is
#: how a settings page starts lying about what the program does.
INTERNAL_KEYS: frozenset[str] = frozenset()


def all_fields() -> list[Field]:
    return [f for category in CATEGORIES for f in category.fields]


def find(key: str) -> Field | None:
    for f in all_fields():
        if f.key == key:
            return f
    return None


def split(key: str) -> tuple[str, str]:
    """Split ``"capture.fps"`` into ``("capture", "fps")``.

    A key with no dot is a top-level config field (``presentation``, ``profile``),
    which is returned with an empty section rather than rejected -- the schema covers
    those too, and demanding a dot made them unrepresentable.
    """
    if "." in key:
        section, _, name = key.partition(".")
        return section, name
    return "", key


def get_value(config: Any, key: str) -> Any:
    section, name = split(key)
    if not section:
        return getattr(config, name, None)
    container = getattr(config, section, None)
    if container is None:
        return None
    if isinstance(container, dict):
        return container.get(name)
    return getattr(container, name, None)


def coerce(field: Field, raw: Any) -> Any:
    """Turn a form value into the type the config expects, or raise ValueError.

    Rejecting a bad value with a reason matters more than accepting it: a settings
    page that silently clamps or ignores input teaches the user that the controls do
    not work.
    """
    if field.kind == "bool":
        if isinstance(raw, bool):
            return raw
        text = str(raw).strip().lower()
        if text in ("true", "1", "yes", "on"):
            return True
        if text in ("false", "0", "no", "off"):
            return False
        raise ValueError(f"{field.label}：需要一个开关值，收到 {raw!r}")

    if field.kind == "int":
        value: Any = int(float(raw))
    elif field.kind == "float":
        value = float(raw)
    elif field.kind == "enum":
        value = str(raw)
        if field.choices and value not in field.choices:
            raise ValueError(
                f"{field.label}：只能取 {'/'.join(field.choices)}，收到 {value!r}"
            )
    elif field.kind == "path_list":
        if isinstance(raw, (list, tuple)):
            value = [str(v) for v in raw]
        elif raw in (None, ""):
            value = []
        else:
            value = [part.strip() for part in str(raw).split(",") if part.strip()]
    elif field.kind == "region":
        text = "" if raw is None else str(raw).strip()
        if not text:
            return None
        from .capture import Region

        Region.parse(text)
        value = text
    else:
        value = "" if raw is None else str(raw)
        if field.kind == "text" and not value and field.default is None:
            return None

    if field.kind in ("int", "float") and field.low is not None and field.high is not None:
        if not (field.low <= value <= field.high):
            raise ValueError(
                f"{field.label}：需在 {field.low}–{field.high}{field.unit} 之间，"
                f"收到 {value}"
            )
    return value


def as_dict(config: Any) -> dict[str, Any]:
    """The whole schema plus current values, for an API response or a form."""
    return {
        "categories": [
            {
                **category.to_dict(),
                "values": {f.key: get_value(config, f.key) for f in category.fields},
            }
            for category in CATEGORIES
        ],
        "live_count": sum(1 for f in all_fields() if f.applies == LIVE),
        "restart_count": sum(1 for f in all_fields() if f.applies == RESTART),
    }


def iter_keys() -> Iterable[str]:
    for f in all_fields():
        yield f.key
