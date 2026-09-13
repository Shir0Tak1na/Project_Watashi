#!/usr/bin/env python3
"""Corpus and rule engine verification (R2 / R3). Headless: no screen, no models.

The corpus engine is the core of this project -- the model only fills grammar around
it -- and until now it had no check of its own. It was verified indirectly, through
the session boundary and through benchmark numbers, which is how two real defects sat
in it unobserved:

1. **A corpus had no target-language dimension.** An English->Chinese vocabulary
   answered a request for Japanese *with Chinese, at full confidence*. Setting the
   target to Japanese and reading Chinese back is the same failure the echo gate
   catches when source == target, and worse, because the text is foreign either way
   so nothing looks wrong.
2. **Rule language matching was string equality.** ``target: "zh-CN"`` did not match a
   request for ``zh``, ``zho_Hans`` or ``zh_CN`` -- all spellings this project accepts
   elsewhere. Typing ``zh`` silently switched off every Chinese rule in the shipped
   rule set, and the rule engine simply looked broken.

Both are asserted here, along with the precedence rules, the entry forms, the
explainability contract and R3's write-back loop (a rule's guess becoming a corpus
entry), so that the next change to this file has something to fail against.

    run.cmd selfcheck_corpus --summary
"""

from __future__ import annotations

import json
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from watashi.checks import Checker  # noqa: E402

from watashi import correct as corrections
from watashi.translate import (
    LAYER_DOMAIN,
    LAYER_GENERAL,
    LAYER_USER,
    CorpusStore,
    CorpusTranslator,
)

ZH = "zh-CN"
JA = "ja"
EN = "en"


def write(directory: Path, name: str, payload: dict) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def store(root: Path, *, rules: list[Path] | None = None) -> CorpusStore:
    return CorpusStore(
        layers={
            LAYER_USER: [root / "user"],
            LAYER_DOMAIN: [root / "domain"],
            LAYER_GENERAL: [root / "general"],
        },
        rule_files=rules or [],
        reload_interval_s=0.0,
    )


def main() -> int:
    check = Checker()
    print("=" * 78)
    print("Corpus and rule engine self check (no screen, no models)")
    print("=" * 78)

    root = Path(tempfile.mkdtemp(prefix="watashi-corpus-"))

    # ---------------------------------------------------------------- #
    check.section("layering: user beats domain beats general")

    write(root / "user", "mine.json", {"sword": "剑(用户)"})
    write(root / "domain", "demo.json", {"sword": "剑(领域)", "realm": "境界"})
    write(root / "general", "base.json", {"sword": "剑(通用)", "the": "这"})
    corpus = store(root)

    check.check(
        "every distinct source term is loaded once, whichever layer it came from",
        corpus.size == 3,
        f"{corpus.size} entries for 3 distinct sources across 3 files",
    )
    check.check(
        "the user layer wins for a term all three declare",
        corpus.translate("sword", ZH).target_text == "剑(用户)",
        corpus.translate("sword", ZH).target_text,
    )
    check.check(
        "the entry knows which layer it came from",
        corpus.translate("sword", ZH).spans[0].origin == "corpus:mine",
        corpus.translate("sword", ZH).spans[0].origin,
    )
    check.check(
        "a user entry is reported at full confidence, domain and general below it",
        corpus.lookup_exact("sword").layer == LAYER_USER,
        corpus.lookup_exact("sword").layer,
    )

    # ---------------------------------------------------------------- #
    check.section("longest match wins inside a layer")

    write(root / "domain", "phrases.json", {"gg": "打得好", "gg wp": "打得好，打得漂亮"})
    corpus.load()
    check.check(
        "a two-word phrase beats the single word it starts with",
        corpus.translate("gg wp", ZH).target_text == "打得好，打得漂亮",
        corpus.translate("gg wp", ZH).target_text,
    )
    check.check(
        "and the single word still works on its own",
        corpus.translate("gg", ZH).target_text == "打得好",
        corpus.translate("gg", ZH).target_text,
    )
    check.check(
        "a term is not matched inside a longer word",
        corpus.translate("eggs", ZH).target_text == "eggs",
        f"{corpus.translate('eggs', ZH).target_text!r}: 'gg' must not match inside 'eggs'",
    )
    check.check(
        "and that non-match is reported as unexplained",
        corpus.translate("eggs", ZH).coverage == 0.0,
        f"coverage={corpus.translate('eggs', ZH).coverage}",
    )

    # ---------------------------------------------------------------- #
    check.section("priority breaks a tie inside one layer")

    write(root / "domain", "low.json", {"dao": "道(低)"})
    write(
        root / "domain",
        "high.json",
        {"dao": {"target": "道(高)", "priority": 10, "pos": "noun", "domain": "cultivation"}},
    )
    corpus.load()
    winner = corpus.lookup_exact("dao")
    check.check(
        "the higher priority wins regardless of file order",
        winner is not None and winner.target == "道(高)",
        f"{winner.target if winner else None}",
    )
    check.check("the richer fields come with it", winner is not None and winner.pos == "noun"
                and winner.domain == "cultivation", f"{winner}")

    # Load the same two files under swapped names: the winner must not depend on the
    # order the directory happens to be walked in.
    swapped = Path(str(root) + "-swapped")
    write(swapped / "user", "keep.json", {})
    write(swapped / "domain", "a_low.json", {"dao": "道(低)"})
    write(swapped / "domain", "b_high.json", {"dao": {"target": "道(高)", "priority": 10}})
    swapped_corpus = store(swapped)
    check.check(
        "and it does not depend on the file walk order",
        swapped_corpus.lookup_exact("dao").target == "道(高)",
        swapped_corpus.lookup_exact("dao").target,
    )

    # ---------------------------------------------------------------- #
    check.section("entry forms: what is accepted, and what is refused")

    forms = Path(str(root) + "-forms")
    write(
        forms / "user",
        "forms.json",
        {
            "_comment": "keys starting with an underscore are metadata, not entries",
            "plain": "平",
            "aliased": {"translation": "别名"},
            "texted": {"text": "文本"},
            "targeted": {"target": "目标"},
            "notarget": {"pos": "noun"},
            "number": 42,
            "nested": {"target": ""},
        },
    )
    forms_corpus = store(forms)
    check.check(
        "the short string form works",
        forms_corpus.translate("plain", ZH).target_text == "平",
    )
    check.check(
        "the 'translation' alias works",
        forms_corpus.translate("aliased", ZH).target_text == "别名",
    )
    check.check(
        "the 'text' alias works",
        forms_corpus.translate("texted", ZH).target_text == "文本",
    )
    check.check(
        "the 'target' key works",
        forms_corpus.translate("targeted", ZH).target_text == "目标",
    )
    check.check(
        "an underscore key is metadata, not a term to translate",
        forms_corpus.lookup_exact("_comment") is None
        and forms_corpus.translate("_comment", ZH).target_text == "_comment",
    )
    check.check(
        "an entry with no target is dropped rather than crashing",
        forms_corpus.lookup_exact("notarget") is None,
    )
    check.check(
        "a non-string value is dropped",
        forms_corpus.lookup_exact("number") is None,
    )
    check.check(
        "an empty target is dropped",
        forms_corpus.lookup_exact("nested") is None,
    )
    check.check("size counts entries, not files", forms_corpus.size == 4, f"{forms_corpus.size}")

    wrapped = Path(str(root) + "-wrapped")
    write(wrapped / "user", "wrapped.json", {"entries": {"void": "虚空"}, "_readme": "..."})
    wrapped_corpus = store(wrapped)
    check.check(
        "the {'entries': {...}} wrapper is accepted",
        wrapped_corpus.translate("void", ZH).target_text == "虚空",
    )
    check.check(
        "and its metadata keys do not become entries",
        wrapped_corpus.size == 1,
        f"{wrapped_corpus.size}",
    )

    # ---------------------------------------------------------------- #
    check.section("the target-language dimension: the bug this check exists for")

    mixed = Path(str(root) + "-mixed")
    write(mixed / "user", "zh.json", {"lang": "zh-CN", "entries": {"sword intent": "剑意"}})
    write(mixed / "user", "ja.json", {"lang": "ja", "entries": {"sword intent": "剣意"}})
    write(mixed / "domain", "neutral.json", {"shared term": "共享"})
    mixed_corpus = store(mixed)

    check.check(
        "an en->zh corpus no longer answers a request for Japanese",
        mixed_corpus.translate("sword intent", JA).target_text != "剑意",
        f"got {mixed_corpus.translate('sword intent', JA).target_text!r}",
    )
    check.check(
        "it answers with the Japanese entry instead",
        mixed_corpus.translate("sword intent", JA).target_text == "剣意",
        mixed_corpus.translate("sword intent", JA).target_text,
    )
    check.check(
        "and with the Chinese entry when Chinese is asked for",
        mixed_corpus.translate("sword intent", ZH).target_text == "剑意",
    )
    check.check(
        "for a language the corpus has nothing for, the source comes back unchanged",
        mixed_corpus.translate("sword intent", EN).target_text == "sword intent",
        mixed_corpus.translate("sword intent", EN).target_text,
    )
    check.check(
        "one source term can carry two languages without one evicting the other",
        mixed_corpus.size == 3
        and mixed_corpus.lookup_exact("sword intent", ZH).target == "剑意"
        and mixed_corpus.lookup_exact("sword intent", JA).target == "剣意",
        f"size={mixed_corpus.size} zh={mixed_corpus.lookup_exact('sword intent', ZH).target!r} "
        f"ja={mixed_corpus.lookup_exact('sword intent', JA).target!r}",
    )
    check.check(
        "an untagged entry is still usable for every target",
        mixed_corpus.translate("shared term", ZH).target_text == "共享"
        and mixed_corpus.translate("shared term", JA).target_text == "共享",
    )
    check.check(
        "the corpus reports which languages it can answer for",
        set(mixed_corpus.languages) == {"zho", "jpn"} and mixed_corpus.has_untagged_entries,
        f"{mixed_corpus.languages} untagged={mixed_corpus.has_untagged_entries}",
    )
    check.check(
        "and says so in one line a UI can show",
        "zho" in mixed_corpus.language_summary() and "未标注" in mixed_corpus.language_summary(),
        mixed_corpus.language_summary(),
    )

    # Language identity, not string identity: the same class of bug as the rules had.
    check.check(
        "an NLLB code as the entry language is understood as that language",
        store(_lang_dir(root, "code", {"lang": "jpn_Jpan", "entries": {"x": "エックス"}}))
        .translate("x", JA)
        .target_text
        == "エックス",
    )
    check.check(
        "'zh' as a target finds an entry tagged 'zh-CN'",
        mixed_corpus.translate("sword intent", "zh").target_text == "剑意",
        mixed_corpus.translate("sword intent", "zh").target_text,
    )
    check.check(
        "'zh-TW' finds it too: refusing would leave Traditional users with nothing",
        mixed_corpus.translate("sword intent", "zh-TW").target_text == "剑意",
        mixed_corpus.translate("sword intent", "zh-TW").target_text,
    )
    check.check(
        "an unknown tag matches only itself, literally",
        store(_lang_dir(root, "odd", {"lang": "klingon", "entries": {"x": "Qapla"}}))
        .translate("x", ZH)
        .target_text
        == "x",
        "an unrecognised language must not leak into every target",
    )

    check.check(
        "an entry's own lang overrides the file's",
        store(
            _lang_dir(
                root,
                "override",
                {"lang": "zh-CN", "entries": {"y": {"target": "ワイ", "lang": "ja"}}},
            )
        )
        .translate("y", JA)
        .target_text
        == "ワイ",
    )

    # The bare form has no metadata layer, so a "lang" key there is just an entry. That
    # is a footgun with an obvious intent, and the engine says so instead of guessing:
    # reading the intent would mean either silently dropping a legitimate entry for the
    # English word "lang" or inventing a rule about which values look like a language.
    bare = _lang_dir(root, "barelang", {"lang": "zh-CN", "sword": "剑"})
    bare_corpus = store(bare)
    check.check(
        "a bare-form 'lang' key is reported rather than guessed at",
        bare_corpus.lookup_exact("lang") is not None
        and not bare_corpus.languages
        and bare_corpus.has_untagged_entries,
        f"entry={bare_corpus.lookup_exact('lang')} languages={bare_corpus.languages}",
    )
    check.check(
        "and the rest of the file still loads",
        bare_corpus.translate("sword", ZH).target_text == "剑",
    )

    # ---------------------------------------------------------------- #
    check.section("lookup_exact is language aware when asked to be")

    check.check(
        "with no language it prefers the language-neutral entry",
        mixed_corpus.lookup_exact("shared term").target == "共享",
    )
    check.check(
        "with one language it returns that language's entry",
        mixed_corpus.lookup_exact("sword intent", JA).target == "剣意"
        and mixed_corpus.lookup_exact("sword intent", ZH).target == "剑意",
    )
    check.check(
        "and falls back to the neutral entry when that language has none",
        mixed_corpus.lookup_exact("shared term", JA).target == "共享",
    )
    check.check(
        "an unknown term is None either way",
        mixed_corpus.lookup_exact("nothing here", JA) is None
        and mixed_corpus.lookup_exact("") is None,
    )

    # ---------------------------------------------------------------- #
    check.section("rules: language identity, and the lookups they make")

    rules_dir = Path(str(root) + "-rules")
    write(rules_dir / "user", "terms.json", {"lang": "zh-CN", "entries": {"void": "虚空"}})
    rule_file = rules_dir / "rules.json"
    write(
        rules_dir,
        "rules.json",
        {
            "rules": [
                {
                    "id": "affix-zh",
                    "type": "affix",
                    "priority": 10,
                    "confidence": 0.55,
                    "target": "zh-CN",
                    "min_stem": 3,
                    "prefixes": {"anti": "反"},
                }
            ]
        },
    )
    rule_corpus = store(rules_dir, rules=[rule_file])
    check.check(
        "a rule tagged 'zh-CN' applies when the target is written 'zh'",
        rule_corpus.translate("antivoid", "zh").target_text == "反虚空",
        f"{rule_corpus.translate('antivoid', 'zh').target_text!r}: "
        "string equality here used to disable every Chinese rule",
    )
    check.check(
        "and for 'zho_Hans', and for 'zh_CN', and for a differently cased 'ZH-cn'",
        all(
            rule_corpus.translate("antivoid", tag).target_text == "反虚空"
            for tag in ("zho_Hans", "zh_CN", "ZH-cn")
        ),
        str([rule_corpus.translate("antivoid", t).target_text for t in ("zho_Hans", "zh_CN", "ZH-cn")]),
    )
    check.check(
        "and still does not apply for Japanese",
        rule_corpus.translate("antivoid", JA).target_text == "antivoid",
        rule_corpus.translate("antivoid", JA).target_text,
    )

    # A rule's own corpus lookups must respect its language, or a Chinese rule would
    # assemble its answer out of Japanese entries.
    both_dir = Path(str(root) + "-bothlang")
    write(both_dir / "user", "zh.json", {"lang": "zh-CN", "entries": {"void": "虚空"}})
    write(both_dir / "user", "ja.json", {"lang": "ja", "entries": {"void": "ヴォイド"}})
    both_rules = both_dir / "rules.json"
    write(
        both_dir,
        "rules.json",
        {
            "rules": [
                {
                    "id": "affix-zh",
                    "type": "affix",
                    "target": "zh-CN",
                    "confidence": 0.55,
                    "min_stem": 3,
                    "prefixes": {"anti": "反"},
                },
                {
                    "id": "affix-ja",
                    "type": "affix",
                    "target": "ja",
                    "confidence": 0.55,
                    "min_stem": 3,
                    "prefixes": {"anti": "アンチ"},
                },
            ]
        },
    )
    both_corpus = store(both_dir, rules=[both_rules])
    check.check(
        "a Chinese rule builds its answer from Chinese entries",
        both_corpus.translate("antivoid", ZH).target_text == "反虚空",
        both_corpus.translate("antivoid", ZH).target_text,
    )
    check.check(
        "and the Japanese rule from Japanese entries",
        both_corpus.translate("antivoid", JA).target_text == "アンチヴォイド",
        both_corpus.translate("antivoid", JA).target_text,
    )

    # ---------------------------------------------------------------- #
    check.section("a broken rule is reported, never fatal")

    broken_dir = Path(str(root) + "-broken")
    write(broken_dir / "user", "terms.json", {"void": "虚空"})
    broken_rules = broken_dir / "rules.json"
    write(
        broken_dir,
        "rules.json",
        {
            "rules": [
                {"id": "bad-regex", "type": "template", "pattern": "([unclosed", "replacement": "x"},
                {"id": "bad-type", "type": "nonsense"},
                {"id": "disabled", "type": "affix", "enabled": False, "prefixes": {"a": "b"}},
                {
                    "id": "good",
                    "type": "affix",
                    "priority": 50,
                    "confidence": 0.5,
                    "min_stem": 3,
                    "prefixes": {"anti": "反"},
                },
            ]
        },
    )
    broken_corpus = store(broken_dir, rules=[broken_rules])
    check.check(
        "the bad regex, the unknown type and the disabled rule are all skipped",
        broken_corpus.rule_ids() == ["good"],
        str(broken_corpus.rule_ids()),
    )
    check.check(
        "and the corpus still loads and translates",
        broken_corpus.translate("void", ZH).target_text == "虚空",
    )
    check.check(
        "the surviving rule still works",
        broken_corpus.translate("antivoid", ZH).target_text == "反虚空",
    )
    check.check(
        "a rule with no language applies to every target",
        "反" in broken_corpus.translate("antivoid", JA).target_text,
        broken_corpus.translate("antivoid", JA).target_text,
    )

    # ---------------------------------------------------------------- #
    check.section("explainability: every span says where it came from")

    outcome = both_corpus.translate("antivoid and void", ZH)
    check.check(
        "the spans cover the whole input, source side",
        "".join(span.source for span in outcome.spans) == "antivoid and void",
        repr("".join(span.source for span in outcome.spans)),
    )
    check.check(
        "every span carries an origin and a confidence",
        all(span.origin and 0.0 <= span.confidence <= 1.0 for span in outcome.spans),
        str([(s.origin, s.confidence) for s in outcome.spans]),
    )
    check.check(
        "corpus and rule spans count as explained, literals do not",
        all(
            span.explained == span.origin.startswith(("corpus:", "rule:"))
            for span in outcome.spans
        ),
    )
    check.check(
        "the trace is readable and names the rule that fired",
        "affix-zh" in outcome.trace() and "corpus:" in outcome.trace(),
        outcome.trace()[:90],
    )
    check.check(
        "coverage is the share of source characters that were explained",
        0.0 < outcome.coverage < 1.0,
        f"coverage={outcome.coverage:.3f} for {outcome.source_text!r}",
    )
    check.check(
        "the transliterate fallback is flagged as low confidence, not as a real answer",
        (lambda o: o.spans[-1].confidence <= 0.15)(both_corpus.translate("warpdrive", ZH)),
        f"{both_corpus.translate('warpdrive', ZH).spans[-1].confidence}",
    )

    check.check(
        "empty input comes back empty rather than raising",
        both_corpus.translate("   ", ZH).source_text == "   ",
    )

    # ---------------------------------------------------------------- #
    check.section("R3's write-back: a rule's guess becomes a corpus entry")

    writeback = Path(str(root) + "-writeback")
    write(writeback / "user", "terms.json", {"lang": "zh-CN", "entries": {"void": "虚空"}})
    wb_rules = writeback / "rules.json"
    write(
        writeback,
        "rules.json",
        {
            "rules": [
                {
                    "id": "affix-en-to-zh",
                    "type": "affix",
                    "target": "zh-CN",
                    "confidence": 0.55,
                    "min_stem": 3,
                    "prefixes": {"anti": "反"},
                }
            ]
        },
    )
    wb_corpus = store(writeback, rules=[wb_rules])
    before = wb_corpus.translate("antivoid", ZH)
    check.check(
        "the rule answers, at its declared low confidence",
        before.target_text == "反虚空" and before.spans[0].confidence == 0.55,
        f"{before.target_text!r} {before.spans[0].confidence}",
    )
    check.check(
        "and the answer is attributed to the rule, not to the corpus",
        before.spans[0].origin == "rule:affix" and before.spans[0].rule_id == "affix-en-to-zh",
        f"{before.spans[0].origin} {before.spans[0].rule_id}",
    )

    # The one-click write-back the requirement asks for: the user accepts the rule's
    # answer, and from then on it is vocabulary rather than inference.
    corrections.Corrections(corrections.corrections_path(_config_for(writeback))).record(
        "antivoid", before.target_text, scope=corrections.SCOPE_TERM
    )
    wb_corpus.load()
    after = wb_corpus.translate("antivoid", ZH)
    check.check(
        "after the write-back the same word is answered by the corpus",
        after.spans[0].origin.startswith("corpus:") and after.spans[0].rule_id is None,
        f"{after.spans[0].origin} rule={after.spans[0].rule_id}",
    )
    check.check(
        "at full confidence instead of the rule's estimate",
        after.spans[0].confidence == 1.0,
        f"{after.spans[0].confidence}",
    )
    check.check(
        "and the answer itself is unchanged",
        after.target_text == before.target_text == "反虚空",
        f"{after.target_text!r} vs {before.target_text!r}",
    )
    check.check(
        "the term now also applies inside other words, which is what term scope means",
        wb_corpus.translate("antivoid and more", ZH).target_text.startswith("反虚空"),
        wb_corpus.translate("antivoid and more", ZH).target_text,
    )

    # ---------------------------------------------------------------- #
    check.section("hot reload and damaged files")

    livedir = Path(str(root) + "-live")
    write(livedir / "user", "one.json", {"lang": "zh-CN", "entries": {"a": "甲"}})
    live = store(livedir)
    check.check("starts with one language", live.languages == ["zho"], str(live.languages))
    write(livedir / "user", "two.json", {"lang": "ja", "entries": {"a": "亜"}})
    live.translate("a", ZH)
    check.check(
        "a new file is picked up, and the language list grows with it",
        live.languages == ["jpn", "zho"] and live.size == 2,
        f"{live.languages} size={live.size}",
    )
    check.check(
        "and the new language answers for itself",
        live.translate("a", JA).target_text == "亜",
    )

    (livedir / "user" / "two.json").write_text("{ not json at all", encoding="utf-8")
    time.sleep(0.01)
    live.load()
    check.check(
        "a damaged file is skipped and the rest of the corpus still loads",
        live.size == 1 and live.translate("a", ZH).target_text == "甲",
        f"size={live.size}",
    )
    check.check(
        "and the damage does not take the process down",
        live.translate("a", JA).target_text == "a",
        "with the ja file gone, Japanese has nothing again",
    )

    # ---------------------------------------------------------------- #
    check.section("the shipped data files declare their language")

    shipped = Path(__file__).resolve().parent
    for label, path, expected in (
        ("demo corpus", shipped / "corpus" / "demo_terms.json", "zh-CN"),
        ("internet slang", shipped.parent / "rules" / "slang" / "internet_slang.json", "zh-CN"),
        ("fiction terms", shipped.parent / "rules" / "fiction" / "fantasy_terms.json", "zh-CN"),
    ):
        payload = json.loads(path.read_text(encoding="utf-8"))
        check.check(
            f"the {label} declares lang={expected}",
            payload.get("lang") == expected and isinstance(payload.get("entries"), dict),
            f"lang={payload.get('lang')!r} entries={type(payload.get('entries')).__name__}",
        )
        check.check(
            f"and the {label} has no stray top-level keys",
            all(k in ("lang", "entries") or k.startswith("_") for k in payload),
            str(sorted(payload)),
        )

    # ---------------------------------------------------------------- #
    check.section("against the live project configuration")

    from watashi.config import AppConfig
    from watashi.session import build_corpus

    project = build_corpus(AppConfig.load())
    check.check(
        "the shipped project corpus loads and every entry declares a language",
        project.size > 80 and project.languages == ["zho"] and not project.has_untagged_entries,
        f"{project.size} entries, languages={project.languages}, "
        f"untagged={project.has_untagged_entries}",
    )
    check.check(
        "so an English line still translates into Chinese",
        project.translate("sword intent", ZH).target_text == "剑意",
        project.translate("sword intent", ZH).target_text,
    )
    check.check(
        "while a Japanese target is honestly left untranslated by the corpus",
        project.translate("sword intent", JA).target_text == "sword intent",
        f"{project.translate('sword intent', JA).target_text!r}: the pipeline turns this "
        "into coverage 0 and lets the model answer, rather than showing Chinese",
    )
    check.check(
        "and the stats a UI shows say which languages the corpus holds",
        "zho" in str(project.language_summary()),
        project.language_summary(),
    )
    check.check(
        "the corpus is wrapped in a translator that reports the same thing",
        "zho" in CorpusTranslator(project).stats().get("corpus_languages", ""),
        str(CorpusTranslator(project).stats().get("corpus_languages")),
    )

    return check.report()


def _lang_dir(root: Path, name: str, payload: dict) -> Path:
    """A one-file user layer declaring ``payload``, for one-line assertions."""
    directory = Path(str(root) + "-" + name)
    write(directory / "user", "terms.json", payload)
    return directory


def _config_for(directory: Path):
    """A minimal config object whose user corpus layer is ``directory``/user."""

    class _Config:
        base_dir = directory

        @staticmethod
        def corpus_dirs(layer: str) -> list[Path]:
            return [directory / "user"] if layer == LAYER_USER else []

    return _Config()


if __name__ == "__main__":
    sys.exit(main())
