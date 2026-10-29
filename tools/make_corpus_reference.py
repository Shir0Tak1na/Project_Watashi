#!/usr/bin/env python3
"""Build the corpus/rules fixture set and record the Python reference.

    .venv/Scripts/python.exe tools/make_corpus_reference.py

Writes (committed, small and deterministic):

    golden/corpus/data/{general,domain,user}/*.json   fixture corpora
    golden/corpus/data/rules.json                     fixture rules
    golden/corpus/cases.jsonl                         120 cases + expected outcome
    golden/corpus/manifest.json                       commits, hashes, counts

Every case carries its own target language, scene and window, because a corpus
answer depends on all three. The expected values come from the real Python
engine (``build_corpus`` + ``CorpusTranslator``), not from hand-written
expectations: the port is judged against behaviour, not against a re-reading of
it.

No timestamps anywhere: the manifest has to be byte-identical on a second run.
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROTOTYPE = ROOT / "prototype"
OUT = ROOT / "golden" / "corpus"
DATA = OUT / "data"

sys.path.insert(0, str(PROTOTYPE))

GENERAL = {
    "lang": "zh-CN",
    "entries": {
        "river": "河",
        "bank": "岸",
        "dragon": "龙",
        "sword": "剑",
        "sect": "宗门",
        "core": "核心",
        "realm": "境界",
        "technique": "功法",
        "aether": "以太",
        "signal": "信号",
        "engine": "引擎",
        "gg": "打得不错",
        "wp": "打得好",
        "noob": "新手",
        "level": "等级",
        "chapter": "章节",
        "myth": "传说",
        "gravity": "重力",
        "cyber": "赛博",
        "punk": "朋克",
        "dark matter": "暗物质",
        "artificial gravity": "人工重力",
        "金丹": {"target": "golden core", "lang": "en"},
        "虚空": {"target": "void", "lang": "en"},
        "剑意": {"target": "sword intent", "lang": "en"},
    },
}

DOMAIN = {
    "entries": {
        "sword": {"target": "剑意", "domain": "fiction"},
        "bank": {"target": "河岸", "domain": "fiction"},
        "core": {"target": "金丹", "domain": "fiction"},
        "gate": {"target": "山门", "domain": "fiction"},
        "realm": {"target": "境界", "domain": "fiction", "when_line": "突破|cultivat"},
    },
}

USER = {
    "entries": {
        "sword": {"target": "本门之剑"},
        "gg": {"target": "干得漂亮"},
        "level": {"target": "层级", "priority": 5},
        "core": [
            {"target": "内丹", "when_line": "丹|pill"},
            {"target": "核心", "when_near": "reactor"},
            {"target": "内核", "priority": 1},
        ],
        "bank": [
            {"target": "银行", "when_line": "转账|account|finance"},
            {"target": "河岸", "when_line": "river|岸边"},
        ],
        "gate": {"target": "宗门大门", "when_window": "Watashi"},
        "signal": [
            {"target": "信号", "priority": 9},
            {"target": "讯号", "priority": 3},
        ],
    }
}

RULES = {
    "rules": [
        {
            "id": "affix-en-to-zh",
            "type": "affix",
            "priority": 10,
            "confidence": 0.55,
            "target": "zh-CN",
            "min_stem": 3,
            "prefixes": {"anti": "反", "un": "不", "cyber": "赛博"},
            "suffixes": {"ness": "性", "less": "无", "ist": "主义者"},
        },
        {
            "id": "morpheme-en-to-zh",
            "type": "morpheme",
            "priority": 30,
            "confidence": 0.45,
            "target": "zh-CN",
            "separator": "",
        },
        {
            "id": "template-realm-zh-to-en",
            "type": "template",
            "priority": 20,
            "confidence": 0.6,
            "target": "en",
            "pattern": "(?P<base>.+)(境|界)",
            "replacement": "{base} Realm",
        },
        {
            "id": "template-sect-zh-to-en",
            "type": "template",
            "priority": 21,
            "confidence": 0.6,
            "target": "en",
            "pattern": "(?P<base>.+)(宗|门)",
            "replacement": "{base} Sect",
        },
        {
            "id": "transliterate-fallback",
            "type": "transliterate",
            "priority": 999,
            "confidence": 0.1,
        },
    ]
}


def write_data() -> None:
    (DATA / "general").mkdir(parents=True, exist_ok=True)
    (DATA / "domain").mkdir(parents=True, exist_ok=True)
    (DATA / "user").mkdir(parents=True, exist_ok=True)
    for name, payload in (
        ("general/general.json", GENERAL),
        ("domain/fiction.json", DOMAIN),
        ("user/user.json", USER),
    ):
        (DATA / name).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    (DATA / "rules.json").write_text(
        json.dumps(RULES, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def cases() -> list[dict]:
    out: list[dict] = []

    def add(case_id: str, text: str, target: str = "zh-CN",
            scene: str = "", window: str = "") -> None:
        out.append({"id": case_id, "text": text, "target": target,
                    "scene": scene, "window": window})

    # 20: layer precedence and priority
    for index, text in enumerate(
        ["sword", "the sword", "gg", "gg wp", "gg wp noob", "level up", "signal lost",
         "chapter one", "myth", "dragon", "sect", "core", "realm", "bank",
         "river bank", "sword and dragon", "gg wp noob gg", "level 42",
         "signal", "core reactor"]
    ):
        add(f"layer-{index:02d}", text)

    # 15: scene
    for index, (text, scene) in enumerate(
        [("bank", "fiction"), ("bank", "finance"), ("bank", ""), ("sword", "fiction"),
         ("core", "fiction"), ("gate", "fiction"), ("bank", "Fiction"), ("sword", "finance"),
         ("realm", "fiction"), ("realm", "finance"), ("the bank", "fiction"),
         ("sword intent", "fiction"), ("dragon", "fiction"), ("core realm", "fiction"),
         ("aether", "fiction")]
    ):
        add(f"scene-{index:02d}", text, scene=scene)

    # 12: when_line
    for index, text in enumerate(
        ["bank account", "bank of the river", "转账失败", "river bank at dawn",
         "finance screen", "账号 account", "岸边", "the bank is closed",
         "core pill", "煮丹", "cultivation breakthrough", "突破境界"]
    ):
        add(f"when-line-{index:02d}", text)

    # 12: when_near
    for index, text in enumerate(
        ["core reactor", "reactor core", "core sample", "the core", "core",
         "pill core", "core pill", "signal core", "core signal reactor",
         "reactor near core", "a core of ice", "core"]
    ):
        add(f"when-near-{index:02d}", text)

    # 8: when_window
    for index, text in enumerate(["gate", "the gate", "gate open", "bank", "sword",
                                  "core", "signal", "level"]):
        add(f"when-window-{index:02d}", text, window="Watashi Overlay")

    # 8: conflicts (same slot twice in the user file, and across layers)
    for index, text in enumerate(["signal", "the signal", "signal lost", "sword",
                                  "the sword", "gg wp", "level 1", "bank"]):
        add(f"conflict-{index:02d}", text)

    # 20: four rule types
    for index, text in enumerate(
        ["antigravity", "ungravity", "gravityness", "gravityless", "cyberpunk",
         "antigravityness"]
    ):
        add(f"affix-{index:02d}", text)
    for index, text in enumerate(
        ["cyberpunk", "dragonpunk", "cyberdragon", "cyberpunkcyber", "punkdragon",
         "dragoncyber"]
    ):
        add(f"morpheme-{index:02d}", text)
    for index, text in enumerate(
        ["玄天境", "玄天界", "玄天宗", "玄天门", "玄天境玄天界", "虚空境"]
    ):
        add(f"template-{index:02d}", text, target="en")
    for index, text in enumerate(["Zorblax", "Xylo", "Quasar", "Vex", "Nyx", "Zorblax Xylo"]):
        add(f"translit-{index:02d}", text, target="en")

    # 9: target languages and passthrough
    for index, (text, target) in enumerate(
        [("bank", "ja"), ("sword", "ja"), ("dragon", "en"), ("aether", "en"),
         ("bank", "zh"), ("sword", "zh-CN"), ("core", "ja"), ("signal", "en"),
         ("gg", "ja")]
    ):
        add(f"target-{index:02d}", text, target=target)

    # 12: edge cases
    for index, text in enumerate(["", "   ", "...", "!!!", "12345", "bank!",
                                  "bank, river", "gg  wp", "gg\twp", "SWORD",
                                  "银行 account", "   bank   "]):
        add(f"edge-{index:02d}", text)

    return out


def engine():
    from watashi.config import AppConfig
    from watashi.session import build_corpus
    from watashi.translate import CorpusTranslator

    config = AppConfig.load()
    config.corpus = {
        "user": [str(DATA / "user")],
        "domain": [str(DATA / "domain")],
        "general": [str(DATA / "general")],
        "auto_reload": False,
        "reload_interval_ms": 500,
    }
    config.rules = {"files": [str(DATA / "rules.json")]}
    corpus = build_corpus(config)
    return CorpusTranslator(corpus)


def digest() -> str:
    hasher = hashlib.sha256()
    for path in sorted(DATA.rglob("*.json")):
        hasher.update(path.relative_to(DATA).as_posix().encode("utf-8"))
        hasher.update(path.read_bytes())
    return hasher.hexdigest()


def git_commit() -> str:
    git_dir = ROOT / ".git"
    head = (git_dir / "HEAD").read_text(encoding="utf-8").strip()
    if head.startswith("ref:"):
        return (git_dir / head.split(":", 1)[1].strip()).read_text(encoding="utf-8").strip()
    return head


def main() -> int:
    write_data()
    selected = cases()
    if len(selected) != 120:
        raise SystemExit(f"expected 120 cases, composed {len(selected)}")
    from watashi.translate import Context

    translator = engine()
    records = []
    for case in selected:
        context = Context(target_lang=case["target"], scene=case["scene"],
                          window=case["window"])
        outcome = translator.translate(case["text"], case["target"], context)
        record = dict(case)
        record["expected"] = {
            "target": outcome.target_text,
            "backend": outcome.backend,
            "coverage": round(outcome.coverage, 6),
            "trace": outcome.trace(),
        }
        records.append(record)

    OUT.mkdir(parents=True, exist_ok=True)
    with (OUT / "cases.jsonl").open("w", encoding="utf-8", newline="\n") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")

    manifest = {
        "schema_version": 1,
        "git_commit": git_commit(),
        "config_digest": digest(),
        "corpus": {"user": "data/user", "domain": "data/domain", "general": "data/general"},
        "rules": "data/rules.json",
        "cases": len(records),
        "cases_sha256": hashlib.sha256((OUT / "cases.jsonl").read_bytes()).hexdigest(),
    }
    (OUT / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(f"wrote {len(records)} cases to {OUT}")
    print(json.dumps(manifest, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
