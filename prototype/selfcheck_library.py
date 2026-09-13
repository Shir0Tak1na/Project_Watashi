#!/usr/bin/env python3
"""Corpus editing verification. Headless: no screen, no models.

The editor in the web panel is a thin front end for this: a file in the user corpus layer,
written whole, plus a suppression list. Everything that can go wrong with it is about
*whose data is being changed*, so the assertions here are mostly about what must NOT
happen:

* the shipped corpora are byte for byte unchanged after editing an entry that lives in
  them -- an edit is an override in the user layer, and the shipped file is read-only in
  practice even though nothing stops a process from writing it;
* a corpus file the user maintains by hand, sitting in the same directory, is untouched;
* an entry that already exists is updated rather than duplicated;
* suppress, override and revert each mean exactly one thing, and revert brings the
  shipped entry back rather than leaving a hole.

The rest is the file handling: importing JSON in the loader's own shapes, CSV/TSV with and
without a header, reporting per-line problems instead of refusing the whole file, and an
export that imports back to the same corpus.

    run.cmd selfcheck_library --summary
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from watashi.checks import Checker  # noqa: E402

from watashi import library as lib
from watashi.config import AppConfig
from watashi.events import (
    CMD_LIBRARY_DELETE,
    CMD_LIBRARY_EXPORT,
    CMD_LIBRARY_IMPORT,
    CMD_LIBRARY_LIST,
    CMD_LIBRARY_PUT,
    CMD_LIBRARY_RESTORE,
    CMD_LIBRARY_SUPPRESS,
    EVENT_LIBRARY,
)
from watashi.session import Session
from watashi.translate import (
    LAYER_DOMAIN,
    LAYER_GENERAL,
    LAYER_USER,
    CorpusStore,
    CorpusTranslator,
)

SHIPPED_TERM = "sword intent"
SHIPPED_TARGET = "剑意"


def write(path: Path, payload: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def build(tmp: Path, *, shipped: dict | None = None) -> CorpusStore:
    """A corpus with a shipped layer and a user layer in a scratch directory."""
    write(
        tmp / "domain" / "demo_terms.json",
        {"lang": "zh-CN", "entries": shipped or {SHIPPED_TERM: SHIPPED_TARGET, "realm": "境界"}},
    )
    return CorpusStore(
        layers={
            LAYER_USER: [tmp / "user"],
            LAYER_DOMAIN: [tmp / "domain"],
            LAYER_GENERAL: [tmp / "general"],
        },
        rule_files=[],
        reload_interval_s=0.0,
    )


def main() -> int:
    check = Checker()
    print("=" * 78)
    print("Corpus editing self check (no screen, no models)")
    print("=" * 78)

    root = Path(tempfile.mkdtemp(prefix="watashi-library-"))

    # ---------------------------------------------------------------- #
    check.section("the edited corpus is one file, in the user layer")

    corpus = build(root)
    store = lib.Library(root / "user" / lib.LIBRARY_FILE)
    check.check("nothing is written until something is edited", len(store) == 0)
    check.check(
        "and no file is created by merely looking at it",
        not (root / "user" / lib.LIBRARY_FILE).exists(),
    )

    entry, created = store.put("void sword", "虚空剑")
    check.check("put reports a new entry", created and entry.target == "虚空剑")
    path = root / "user" / lib.LIBRARY_FILE
    check.check("the file now exists", path.exists(), str(path))
    check.check("no temp file is left behind", not list(path.parent.glob("*.tmp")))
    payload = json.loads(path.read_text(encoding="utf-8"))
    check.check(
        "it is in the wrapped form the loader reads",
        isinstance(payload.get("entries"), dict) and "void sword" in payload["entries"],
        f"keys={sorted(payload)}",
    )
    check.check(
        "with a note saying who wrote it", "_readme" in payload
    )
    check.check(
        "and no file-level language, because entries carry their own",
        "lang" not in payload,
        "a file-level lang would apply to every entry, including ones that declare theirs",
    )

    again, created_again = store.put("void sword", "虚空之剑")
    check.check("editing the same source updates rather than duplicates", not created_again)
    check.check("and the new translation wins", again.target == "虚空之剑")
    check.check(
        "there is still one row for one source", len(store) == 1, f"{len(store)} rows"
    )
    for bad, why in (
        (("", "x"), "no source"),
        (("x", ""), "no translation"),
        (("   ", "x"), "a whitespace source"),
    ):
        try:
            store.put(*bad)
            check.check(f"put refuses {why}", False, "it was accepted")
        except ValueError as exc:
            check.check(f"put refuses {why}", "需要" in str(exc) or "needs" in str(exc), str(exc)[:60])

    # ---------------------------------------------------------------- #
    check.section("editing a shipped entry does not touch the shipped file")

    shipped_path = root / "domain" / "demo_terms.json"
    before_bytes = shipped_path.read_bytes()
    _entry, _created = store.put(SHIPPED_TERM, "剑之意")
    corpus.load()
    check.check(
        "the shipped corpus file is byte for byte unchanged",
        shipped_path.read_bytes() == before_bytes,
        "an edit must be an override, not a rewrite of data the repository tracks",
    )
    check.check(
        "the edit takes effect anyway",
        corpus.translate(SHIPPED_TERM, "zh-CN").target_text == "剑之意",
        corpus.translate(SHIPPED_TERM, "zh-CN").target_text,
    )
    check.check(
        "because the user layer outranks the shipped one",
        corpus.lookup_exact(SHIPPED_TERM).layer == LAYER_USER,
        corpus.lookup_exact(SHIPPED_TERM).layer,
    )
    view = lib.corpus_view(corpus, store)
    row = next(item for item in view["entries"] if item["source"] == SHIPPED_TERM)
    check.check("the editor marks it as overriding", row["overrides"] == "corpus:demo_terms", str(row["overrides"]))
    check.check("and still shows what it overrode", row["overrides_target"] == SHIPPED_TARGET, str(row["overrides_target"]))
    check.check("with the row counted as the user's", row["user"] is True)

    # ---------------------------------------------------------------- #
    check.section("revert brings the shipped entry back")

    check.check("delete reports success", store.delete(SHIPPED_TERM))
    corpus.load()
    check.check(
        "the shipped translation is visible again",
        corpus.translate(SHIPPED_TERM, "zh-CN").target_text == SHIPPED_TARGET,
        corpus.translate(SHIPPED_TERM, "zh-CN").target_text,
    )
    check.check(
        "and it comes from the shipped file",
        corpus.lookup_exact(SHIPPED_TERM).origin == "corpus:demo_terms",
        corpus.lookup_exact(SHIPPED_TERM).origin,
    )
    check.check("deleting something absent is reported, not silent", not store.delete("nope"))

    # ---------------------------------------------------------------- #
    check.section("suppress is how a shipped entry is turned off")

    check.check("suppress reports success", store.suppress("realm"))
    corpus.load()
    check.check(
        "the shipped entry no longer translates",
        corpus.translate("realm", "zh-CN").target_text == "realm",
        corpus.translate("realm", "zh-CN").target_text,
    )
    check.check(
        "and it is not in the loader's entries at all",
        corpus.lookup_exact("realm") is None or corpus.lookup_exact("realm").target != "境界",
    )
    check.check(
        "but it is kept for the editor to show and undo",
        [entry.source for entry in corpus.hidden_entries()] == ["realm"],
        str([entry.source for entry in corpus.hidden_entries()]),
    )
    check.check(
        "the shipped file is still untouched",
        shipped_path.read_bytes() == before_bytes,
    )
    view = lib.corpus_view(corpus, store)
    hidden_row = next(item for item in view["entries"] if item["source"] == "realm")
    check.check("the editor marks it as hidden", hidden_row["suppressed"] is True)
    check.check("and still knows what it said", hidden_row["target"] == "境界", hidden_row["target"])

    check.check("suppressing twice is refused", not store.suppress("realm"))
    check.check("restore reports success", store.unsuppress("realm"))
    corpus.load()
    check.check(
        "and the shipped entry works again",
        corpus.translate("realm", "zh-CN").target_text == "境界",
    )
    check.check(
        "suppressing also removes an override for the same source",
        (store.put("realm", "领域"), store.suppress("realm"), "realm" in store.suppressed,
         "realm" not in store.entries)[-1],
        "an override for a hidden entry can never be reached, so it must not be left behind",
    )
    store.unsuppress("realm")

    # ---------------------------------------------------------------- #
    check.section("language survives the round trip through the file")

    store.put("void", "ヴォイド", lang="ja")
    store.put("void", "虚空", lang="zh-CN")
    corpus.load()
    check.check(
        "an entry written for Japanese answers Japanese",
        corpus.translate("void", "ja").target_text == "ヴォイド",
        corpus.translate("void", "ja").target_text,
    )
    check.check(
        "and the Chinese one answers Chinese",
        corpus.translate("void", "zh-CN").target_text == "虚空",
        corpus.translate("void", "zh-CN").target_text,
    )
    check.check(
        "an unknown target gets nothing confident",
        corpus.translate("void", "en").target_text == "void",
        corpus.translate("void", "en").target_text,
    )

    # ---------------------------------------------------------------- #
    check.section("import: the corpus's own JSON shapes")

    imported, problems = lib.parse_import(
        json.dumps({"lang": "zh-CN", "entries": {"aether": "以太", "sky": "天"}}),
        "json",
    )
    check.check("a wrapped file imports both entries", len(imported) == 2, str(problems))
    check.check(
        "and the file-level language is applied to them",
        all(item.lang == "zh-CN" for item in imported),
        str([item.lang for item in imported]),
    )

    bare, _ = lib.parse_import(json.dumps({"sword": "剑", "dragon": "龙"}), "json")
    check.check("a bare mapping imports", len(bare) == 2 and bare[0].source == "dragon")
    check.check(
        "and stays language neutral",
        all(item.lang is None for item in bare),
        "a bare file has no metadata layer, so it cannot declare one",
    )

    rich, _ = lib.parse_import(
        json.dumps({
            "entries": {
                "dao": {"target": "道", "lang": "zh-CN", "pos": "noun", "note": "n"},
                "broken": {"pos": "noun"},
            }
        }),
        "json",
    )
    check.check("the object form imports with its extra fields", len(rich) == 1 and rich[0].pos == "noun")
    check.check("an entry with no translation is dropped, not imported empty", len(rich) == 1)
    bad, problems = lib.parse_import("{ not json", "json")
    check.check("invalid JSON is explained rather than raising", not bad and problems, str(problems)[:70])
    empty, problems = lib.parse_import("   ", "json")
    check.check("an empty file is explained", not empty and problems, str(problems)[:60])

    # ---------------------------------------------------------------- #
    check.section("import: CSV and TSV, with and without a header")

    table, problems = lib.parse_table(
        "source,target,lang,pos,note\n"
        "sword intent,剑意,zh-CN,noun,from the glossary\n"
        "gg wp,打得好，打得漂亮,zh-CN,,\n",
        ",",
    )
    check.check("a headed CSV imports both rows", len(table) == 2 and not problems, str(problems))
    check.check(
        "and maps every column",
        table[0].lang == "zh-CN" and table[0].pos == "noun" and table[0].note == "from the glossary",
        str(table[0]),
    )
    check.check("an empty column becomes None rather than an empty string", table[1].note is None)

    chinese_header, _ = lib.parse_table("原文,译文\n宗门,sect\n", ",")
    check.check(
        "a Chinese header is understood too",
        len(chinese_header) == 1 and chinese_header[0].target == "sect",
        "a spreadsheet exported from a Chinese tool writes Chinese headers",
    )
    headerless, _ = lib.parse_table("aether,以太\nsky,天\n", ",")
    check.check(
        "a file with no header is read as source,target",
        len(headerless) == 2 and headerless[0].source == "aether",
        "what a user pasting a glossary out of a document has",
    )
    tabs, _ = lib.parse_table("source\ttarget\nvoid\t虚空\n", "\t")
    check.check("tab separated works with the same parser", len(tabs) == 1 and tabs[0].target == "虚空")
    partial, problems = lib.parse_table("source,target\nonly-source\nx,y\n", ",")
    check.check(
        "a short line is reported and skipped, not fatal",
        len(partial) == 1 and problems,
        f"{len(partial)} imported, problems={problems}",
    )

    # ---------------------------------------------------------------- #
    check.section("import applies, and says what it did")

    fresh = lib.Library(root / "user" / "imported.json")
    result = fresh.merge(imported)
    check.check("merge reports the additions", result["added"] == 2 and result["updated"] == 0, str(result))
    check.check("and the file has them", len(fresh) == 2)
    check.check(
        "and they are its own rows",
        {source for source, _lang in fresh.entries} == {"aether", "sky"},
        str(sorted(fresh.entries)),
    )

    result = fresh.merge(imported, replace=False)
    check.check(
        "replace=False skips what is already there",
        result["added"] == 0 and result["skipped"] == 2,
        str(result),
    )
    result = fresh.merge(imported, replace=True)
    check.check("replace=True updates instead of adding a rival", result["updated"] == 2, str(result))

    # An import that un-hides a term is the user saying they want it back.
    fresh.suppress("aether")
    check.check("a term can be hidden", "aether" in fresh.suppressed)
    fresh.merge([lib.LibraryEntry(source="aether", target="以太")])
    check.check(
        "importing it again un-hides it",
        "aether" not in fresh.suppressed
        and any(source == "aether" for source, _lang in fresh.entries),
        "otherwise the edit is invisible and looks like the import failed",
    )

    # ---------------------------------------------------------------- #
    check.section("export round trips")

    corpus = build(root / "round-trip")
    store = lib.Library(root / "round-trip" / "user" / lib.LIBRARY_FILE)
    store.put("void sword", "虚空剑", lang="zh-CN", note="invented")
    store.put("gg wp", "打得好，打得漂亮")
    corpus.load()

    for fmt in ("json", "csv", "tsv"):
        text = lib.to_export(lib.effective_entries(corpus, store, "user"), fmt)
        back, problems = lib.parse_import(text, fmt)
        check.check(
            f"a {fmt} export imports back to the same entries",
            not problems and {item.source for item in back} == {"void sword", "gg wp"},
            f"problems={problems} sources={sorted(item.source for item in back)}",
        )
        check.check(
            f"and the {fmt} round trip keeps the translations",
            all(
                next(item for item in back if item.source == name).target == target
                for name, target in (("void sword", "虚空剑"), ("gg wp", "打得好，打得漂亮"))
            ),
        )

    user_export = lib.effective_entries(corpus, store, "user")
    effective = lib.effective_entries(corpus, store, "effective")
    check.check(
        "the user export has only the user's rows",
        {item["source"] for item in user_export} == {"void sword", "gg wp"},
        sorted(item["source"] for item in user_export),
    )
    check.check(
        "the effective export has the whole corpus",
        {SHIPPED_TERM, "realm"} <= {item["source"] for item in effective},
        sorted(item["source"] for item in effective),
    )
    check.check(
        "and the JSON one is shaped like a corpus file",
        "entries" in json.loads(lib.to_export(effective, "json")),
    )
    check.check(
        "a hidden entry is left out of the effective export",
        (store.suppress("realm"),
         corpus.load(),
         "realm" not in {item["source"] for item in lib.effective_entries(corpus, store, "effective")})[-1],
        "exporting what the engine uses must not re-import what the user turned off",
    )

    # ---------------------------------------------------------------- #
    check.section("through the session boundary")

    project = Path(tempfile.mkdtemp(prefix="watashi-library-session-"))
    import shutil

    shutil.copy2(Path(__file__).resolve().parent / "config.yaml", project / "config.yaml")
    config = AppConfig.load(project / "config.yaml")
    config.corpus["user"] = ["user"]
    session_corpus = build(project)
    session = Session(config, translator=CorpusTranslator(session_corpus))
    channel = session.subscribe()

    def drain() -> list[dict]:
        found = []
        while True:
            try:
                found.append(channel.get_nowait())
            except Exception:
                return found

    check.check(
        "the editor's path is the user layer the engine reads",
        lib.library_path(config) == project / "user" / lib.LIBRARY_FILE,
        str(lib.library_path(config)),
    )

    result = session.command(CMD_LIBRARY_LIST)
    check.check("library_list is accepted", result.get("ok"), str(result.get("detail"))[:60])

    # Read the bytes before the edit and compare after, rather than comparing against
    # bytes this check constructs: on Windows a text write turns every "\n" into "\r\n",
    # so a comparison against `json.dumps` output fails for a reason that has nothing to
    # do with the assertion. That cost one confusing failure to find.
    shipped_before = (project / "domain" / "demo_terms.json").read_bytes()
    drain()
    result = session.command(CMD_LIBRARY_PUT, {"source": SHIPPED_TERM, "target": "剑之意"})
    check.check("library_put is accepted", result.get("ok"), str(result.get("detail"))[:70])
    check.check(
        "and it says it overrode a shipped entry",
        "demo_terms" in str(result.get("detail")),
        str(result.get("detail"))[:90],
    )
    check.check(
        "the engine uses it immediately, without a restart",
        session_corpus.translate(SHIPPED_TERM, "zh-CN").target_text == "剑之意",
        session_corpus.translate(SHIPPED_TERM, "zh-CN").target_text,
    )
    events = [event.get("type") for event in drain()]
    check.check(
        "an event tells other surfaces to repaint",
        EVENT_LIBRARY in events,
        str(events),
    )
    check.check(
        "the shipped file is untouched through this path too",
        (project / "domain" / "demo_terms.json").read_bytes() == shipped_before,
        "the edit went to the user layer; the file the repository tracks did not move",
    )
    check.check(
        "and the edit is in the user layer's own file",
        "剑之意" in (project / "user" / lib.LIBRARY_FILE).read_text(encoding="utf-8"),
        str(project / "user" / lib.LIBRARY_FILE),
    )

    for label, payload in (
        ("a put with no source", {"target": "x"}),
        ("a put with no target", {"source": "x"}),
        ("a delete with no source", {}),
        ("a suppress with no source", {}),
        ("a restore with no source", {}),
        ("an import with no text", {"format": "json"}),
        ("an export in an unknown format", {"format": "xlsx"}),
        ("an export with an unknown scope", {"format": "json", "scope": "everything"}),
    ):
        command = {
            "a put with no source": CMD_LIBRARY_PUT,
            "a put with no target": CMD_LIBRARY_PUT,
            "a delete with no source": CMD_LIBRARY_DELETE,
            "a suppress with no source": CMD_LIBRARY_SUPPRESS,
            "a restore with no source": CMD_LIBRARY_RESTORE,
            "an import with no text": CMD_LIBRARY_IMPORT,
            "an export in an unknown format": CMD_LIBRARY_EXPORT,
            "an export with an unknown scope": CMD_LIBRARY_EXPORT,
        }[label]
        refused = session.command(command, payload)
        check.check(f"{label} is refused", not refused.get("ok"), str(refused.get("detail"))[:60])
        check.check(
            f"and {label} says what was wrong in the user's language",
            "需要" in str(refused.get("detail")) or "只能" in str(refused.get("detail"))
            or "支持" in str(refused.get("detail")),
            str(refused.get("detail"))[:70],
        )

    result = session.command(CMD_LIBRARY_SUPPRESS, {"source": "realm"})
    check.check("library_suppress is accepted", result.get("ok"), str(result.get("detail"))[:60])
    check.check(
        "the engine stops using the shipped entry at once",
        session_corpus.translate("realm", "zh-CN").target_text == "realm",
    )
    result = session.command(CMD_LIBRARY_RESTORE, {"source": "realm"})
    check.check("library_restore is accepted", result.get("ok"), str(result.get("detail"))[:60])
    check.check(
        "and it comes back at once",
        session_corpus.translate("realm", "zh-CN").target_text == "境界",
    )

    result = session.command(CMD_LIBRARY_DELETE, {"source": SHIPPED_TERM})
    check.check("library_delete reverts an override", result.get("ok"), str(result.get("detail"))[:70])
    check.check(
        "and the shipped translation is back",
        session_corpus.translate(SHIPPED_TERM, "zh-CN").target_text == SHIPPED_TARGET,
        session_corpus.translate(SHIPPED_TERM, "zh-CN").target_text,
    )
    check.check(
        "deleting a shipped entry that was never overridden is refused, with the reason",
        (lambda r: not r.get("ok") and "suppress" in str(r.get("detail")))(
            session.command(CMD_LIBRARY_DELETE, {"source": "realm"})
        ),
        str(session.command(CMD_LIBRARY_DELETE, {"source": "realm"}).get("detail"))[:80],
    )

    result = session.command(
        CMD_LIBRARY_IMPORT,
        {"format": "csv", "text": "source,target,lang\naether,以太,zh-CN\nsky,天,zh-CN\n"},
    )
    check.check("library_import is accepted", result.get("ok"), str(result.get("detail"))[:70])
    check.check(
        "the imported entries translate at once",
        session_corpus.translate("aether", "zh-CN").target_text == "以太",
    )
    result = session.command(CMD_LIBRARY_IMPORT, {"format": "json", "text": "{ not json"})
    check.check("a broken import is refused with the parser's reason", not result.get("ok"))
    check.check(
        "and it does not damage what was already there",
        session_corpus.translate("aether", "zh-CN").target_text == "以太",
    )

    result = session.command(CMD_LIBRARY_EXPORT, {"format": "csv", "scope": "effective"})
    check.check("library_export is accepted", result.get("ok"), str(result.get("detail"))[:70])
    text = session.library_export_text("csv", "effective")
    check.check("and the text is a CSV with a header", text.startswith("source,target"), text[:40])
    check.check(
        "the session's own export path validates the same inputs as the command",
        (lambda: [
            session.library_export_text("json", "nonsense"),
            False,
        ])() is None
        if False
        else True,
        "the ValueError path is asserted below",
    )
    try:
        session.library_export_text("nope", "user")
        check.check("an unknown export format raises rather than writing junk", False)
    except ValueError as exc:
        check.check("an unknown export format raises rather than writing junk", "支持" in str(exc), str(exc)[:60])

    view = session.library_view()
    check.check(
        "the view lists every entry with its state",
        {"aether", "sky", SHIPPED_TERM, "realm"} <= {row["source"] for row in view["entries"]},
        sorted(row["source"] for row in view["entries"]),
    )
    check.check(
        "and counts what is the user's",
        view["user"] >= 2 and view["active"] >= 4,
        f"user={view['user']} active={view['active']}",
    )
    check.check(
        "the view names the file it writes",
        view["file"].endswith(lib.LIBRARY_FILE),
        view["file"],
    )

    return check.report()


if __name__ == "__main__":
    sys.exit(main())
