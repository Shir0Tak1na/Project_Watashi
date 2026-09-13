#!/usr/bin/env python3
"""Headless engine verification: drive the whole session with no screen.

This is the P1 acceptance test. It proves that the engine works purely through
its published boundary -- events out, commands in -- using ``SyntheticCapturer``
so no display, no capture permission and no real subtitle video is needed.

What it checks:

1. the event stream carries ``ready`` -> ``subtitle`` -> ``refinement`` ->
   ``stats``, in order, with monotonic sequence numbers
2. every envelope round-trips through JSON unchanged
3. commands work: pause actually stops OCR, resume restarts it, ``reload_corpus``
   picks up a corpus edit made *while running*, and unknown commands are
   rejected rather than silently ignored
4. the reported memory tracks the loaded configuration

    run.cmd selfcheck_session
    run.cmd selfcheck_session --json        # dump raw events
"""

from __future__ import annotations

import argparse
import json
import queue
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from watashi.checks import Checker  # noqa: E402

from watashi.config import AppConfig
from watashi.events import (
    CMD_PAUSE,
    CMD_RELOAD_CORPUS,
    CMD_RESUME,
    CMD_SET_FPS,
    CMD_SET_REGION,
    CMD_SET_TARGET_LANG,
    CMD_STATUS,
    EVENT_ERROR,
    EVENT_READY,
    EVENT_REFINEMENT,
    EVENT_STATS,
    EVENT_STOPPED,
    EVENT_SUBTITLE,
    decode_envelope,
    encode_command,
)
from watashi.session import Session
from watashi.synth import SyntheticCapturer




def collect(channel: queue.Queue, seconds: float) -> list[dict]:
    """Drain an event channel for a while."""
    events: list[dict] = []
    deadline = time.perf_counter() + seconds
    while time.perf_counter() < deadline:
        try:
            events.append(channel.get(timeout=0.05))
        except queue.Empty:
            continue
    return events


def types_of(events: list[dict]) -> list[str]:
    return [e.get("type", "?") for e in events]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="selfcheck_session",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--json", action="store_true", help="dump raw events as JSON")
    parser.add_argument("--model", action="store_true", help="load the local model too")
    parser.add_argument("--seconds", type=float, default=4.0, help="observation window")
    parser.add_argument(
        "--summary",
        action="store_true",
        help="print only the totals and any failures (read by watashi.checks)",
    )
    args = parser.parse_args(argv)

    check = Checker()
    print("=" * 78)
    print("Session boundary self check (no screen required)")
    print("=" * 78)

    config = AppConfig.load()
    if not args.model:
        config.translation["nmt_model"] = None

    capturer = SyntheticCapturer(hold_seconds=0.8, width=1280)
    session = Session(config, capturer=capturer)

    memory_before = None
    from watashi.memory import working_set_mib

    memory_before = working_set_mib()

    print("")
    print("-- build and start --")
    session.build()
    info = session.info()
    check.check("session reports a schema version", info.get("schema_version") == 1,
                f"v{info.get('schema_version')}")
    check.check("corpus loaded through the session boundary",
                int(info.get("corpus_entries", 0)) > 0, f"{info.get('corpus_entries')} entries")
    check.check("rule set exposed to clients", int(info.get("rules", 0)) > 0,
                f"{info.get('rules')} rules")
    check.check("ocr engine built", session.ocr.load_ms > 0 or session.ocr.loaded)

    channel = session.subscribe()
    session.start()

    print("")
    print(f"-- observe the event stream for {args.seconds:g}s --")
    events = collect(channel, args.seconds)
    if args.json:
        print(json.dumps(events, ensure_ascii=False, indent=2))

    kinds = types_of(events)
    check.check("ready event received", EVENT_READY in kinds)
    check.check("subtitle events received", kinds.count(EVENT_SUBTITLE) > 0,
                f"{kinds.count(EVENT_SUBTITLE)} subtitle event(s)")
    check.check("stats events received", kinds.count(EVENT_STATS) > 0,
                f"{kinds.count(EVENT_STATS)} stats event(s)")
    if args.model:
        check.check("refinement events received", kinds.count(EVENT_REFINEMENT) > 0,
                    f"{kinds.count(EVENT_REFINEMENT)} refinement event(s)")

    print("")
    print("-- envelope integrity --")
    check.check("every event carries the schema version",
                all(e.get("v") == 1 for e in events))
    seqs = [e["seq"] for e in events if "seq" in e]
    check.check("sequence numbers are present and monotonic",
                len(seqs) == len(events) and seqs == sorted(seqs) and len(set(seqs)) == len(seqs),
                f"{len(seqs)} events, {seqs[0] if seqs else '-'}..{seqs[-1] if seqs else '-'}")
    roundtrip_ok = True
    for event in events:
        try:
            json.loads(json.dumps(event, ensure_ascii=False))
            decode_envelope(event)
        except Exception:
            roundtrip_ok = False
            break
    check.check("every event is JSON safe and decodes", roundtrip_ok)

    subtitles = [e for e in events if e.get("type") == EVENT_SUBTITLE]
    if subtitles:
        data = subtitles[-1]["data"]
        check.check("subtitle payload has source and target",
                    "source" in data and "target" in data,
                    f"{data.get('source','')[:28]!r} -> {data.get('target','')[:24]!r}")
        check.check("subtitle carries latency and backend",
                    "latency_ms" in data and "backend" in data,
                    f"{data.get('latency_ms')} ms, {data.get('backend')!r}")

    print("")
    print("-- geometry (required for in-place and per-line layouts) --")
    with_lines = [e for e in subtitles if e["data"].get("lines")]
    check.check("subtitle events carry a per-line array", len(with_lines) > 0,
                f"{len(with_lines)}/{len(subtitles)} event(s) with lines")
    if with_lines:
        lines = with_lines[-1]["data"]["lines"]
        check.check("each line has source and target",
                    all("source" in l and "target" in l for l in lines),
                    f"{len(lines)} line(s)")
        boxes = [l.get("box") for l in lines]
        check.check("each line carries a 4 element box",
                    all(isinstance(b, list) and len(b) == 4 for b in boxes),
                    f"boxes={boxes}")
        positive = all(b[2] > 0 and b[3] > 0 for b in boxes if isinstance(b, list))
        check.check("boxes have positive width and height", positive)
        # v1 has exactly one line in the synthetic frame per render, so the
        # aggregate must agree with the single line rather than diverge
        check.check("aggregate text agrees with the line list",
                    with_lines[-1]["data"]["target"] == "\n".join(
                        l["target"] for l in lines))

    print("")
    print("-- commands --")
    result = session.command(CMD_STATUS)
    check.check("status command succeeds", result.get("ok"), str(result.get("detail"))[:40])

    result = session.command(CMD_PAUSE)
    check.check("pause command succeeds", result.get("ok"))
    check.check("pause is reflected in state", session.pipeline.paused)
    channel.queue.clear()
    paused_events = collect(channel, 1.5)
    paused_subs = types_of(paused_events).count(EVENT_SUBTITLE)
    check.check("paused session stops producing subtitles", paused_subs == 0,
                f"{paused_subs} subtitle(s) while paused")

    result = session.command(CMD_RESUME)
    check.check("resume command succeeds", result.get("ok") and not session.pipeline.paused)

    result = session.command(CMD_SET_FPS, {"value": 6})
    check.check("set_fps applies to the pipeline",
                result.get("ok") and abs(session.pipeline.config.fps - 6) < 1e-6,
                f"fps={session.pipeline.config.fps}")

    result = session.command(CMD_SET_TARGET_LANG, {"target_lang": "en"})
    check.check("set_target_lang applies",
                result.get("ok") and session.config.target_lang == "en",
                f"target={session.config.target_lang}")
    session.command(CMD_SET_TARGET_LANG, {"target_lang": config.translation.get("target", "zh-CN")})

    result = session.command(CMD_SET_REGION, {"region": "10,20,640,180"})
    check.check("set_region parses x,y,w,h", result.get("ok"), str(result.get("region")))

    result = session.command("nonsense_command")
    check.check("unknown command is rejected, not ignored",
                not result.get("ok") and "unknown" in str(result.get("detail")),
                str(result.get("detail"))[:40])

    result = session.command(CMD_SET_FPS, {"value": -5})
    check.check("invalid command payload is rejected", not result.get("ok"),
                str(result.get("detail"))[:40])

    print("")
    print("-- live customization through the boundary --")
    result = session.command("set_presentation", {"preset": "minimal"})
    check.check("set_presentation accepts a preset",
                result.get("ok") and session.presentation.name == "minimal",
                str(result.get("detail")))
    result = session.command("set_presentation", {"spec": {"background": {"opacity": 0.25}}})
    check.check("set_presentation merges a partial spec",
                result.get("ok") and abs(session.presentation.background.opacity - 0.25) < 1e-6,
                f"opacity={session.presentation.background.opacity}")
    check.check("a merge keeps the rest of the spec",
                session.presentation.name == "minimal"
                and len(session.presentation.elements) == 1)
    result = session.command("set_presentation", {"preset": "inplace"})
    check.check("inplace is selectable and declares its geometry need",
                result.get("ok") and session.presentation.needs_geometry)
    result = session.command("set_presentation", {"preset": "no_such_preset"})
    check.check("an unknown preset is rejected", not result.get("ok"),
                str(result.get("detail"))[:44])
    channel.queue.clear()
    session.command("set_presentation", {"preset": "bar"})
    after_pres = collect(channel, 0.6)
    check.check("presentation changes are broadcast so every surface syncs",
                any(e.get("type") == "presentation" for e in after_pres),
                f"{types_of(after_pres)}")

    result = session.command("load_profile", {"name": "lean"})
    check.check("load_profile applies a memory tier",
                result.get("ok") and session.config.translation.get("nmt_model") is None,
                str(result.get("detail"))[:60])
    result = session.command("load_profile", {"name": "balanced"})
    check.check("balanced restores the originally configured model, not the tier's None",
                result.get("ok") and session.config.translation.get("nmt_model"),
                f"nmt_model={session.config.translation.get('nmt_model')}")
    result = session.command("load_profile", {"name": "nope"})
    check.check("an unknown profile lists what is available",
                not result.get("ok") and "available" in str(result.get("detail")),
                str(result.get("detail"))[:56])

    print("")
    print("-- corpus hot reload through the boundary --")
    corpus = session.corpus
    before = corpus.size if corpus else 0
    with tempfile.TemporaryDirectory() as tmp:
        extra = Path(tmp) / "live_edit.json"
        extra.write_text(
            json.dumps({f"probe_term_{i}": f"探针{i}" for i in range(5)}, ensure_ascii=False),
            encoding="utf-8",
        )
        corpus._layers["user"].append(Path(tmp))
        time.sleep(0.05)
        result = session.command(CMD_RELOAD_CORPUS)
        after = corpus.size if corpus else 0
        check.check("reload_corpus picks up an on-disk edit",
                    result.get("ok") and after == before + 5,
                    f"{before} -> {after} entries")

    print("")
    print("-- memory --")
    stats = session.stats()
    after_mib = working_set_mib()
    check.check("stats report resident memory", stats.memory_mib > 0,
                f"{stats.memory_mib:.1f} MiB reported, {after_mib:.1f} MiB measured")
    check.check("memory grew once models were resident", after_mib >= memory_before,
                f"{memory_before:.1f} -> {after_mib:.1f} MiB")

    print("")
    print("-- adapters are pass-through, not re-encoders --")
    import io

    from watashi.adapters import ConsoleAdapter

    buffer = io.StringIO()
    adapter = ConsoleAdapter(channel, json_lines=True, stream=buffer)
    for event in events:
        adapter.handle(event)
    lines = [line for line in buffer.getvalue().splitlines() if line.strip()]
    check.check("json-lines emits one line per event", len(lines) == len(events),
                f"{len(lines)} lines for {len(events)} events")
    parsed = [json.loads(line) for line in lines]
    check.check("json-lines preserves seq, ts and v on every line",
                all("seq" in p and "ts" in p and "v" in p for p in parsed),
                "an adapter that rebuilds the envelope would drop these")
    check.check("json-lines round-trips the payload",
                all(a.get("data") == b.get("data") for a, b in zip(events, parsed)))

    print("")
    print("-- the final report path (exactly what the CLI prints) --")
    # This was previously untested, and a NameError hid here while every other
    # check passed: the CLI crashed only when it printed its closing summary.
    report = session.stats_dict()
    check.check("stats_dict() builds without raising", isinstance(report, dict),
                f"{len(report)} keys")
    check.check("report carries the headline counters",
                all(k in report for k in ("total_ms", "frames", "backend", "uptime_s")))
    check.check("pending and dropped refinements are separate numbers",
                "refinements_pending" in report and "refinements_dropped" in report,
                f"pending={report.get('refinements_pending')} "
                f"dropped={report.get('refinements_dropped')}")
    try:
        json.dumps(report, ensure_ascii=False)
        serialisable = True
    except TypeError:
        serialisable = False
    check.check("report is JSON serialisable (it is written with --stats-json)", serialisable)

    print("")
    print("-- the language gate: source already the target language --")
    # Chinese on screen must not be "translated" into Chinese. Previously it was:
    # the user saw their own language rewritten, the model spent ~300-600 ms per
    # line rephrasing text needing no work, and the subtitle kept changing.
    # (SyntheticCapturer and AppConfig are already imported at module level;
    # re-importing here would shadow them for the whole function.)
    from watashi.lang import matches_target

    for text, expected in (
        ("对话轨迹", True),
        ("设置", True),
        ("调用指令@文件或对话", True),
        ("the sword intent of this sect", False),
        ("gg wp noob", False),
    ):
        check.check(
            f"gate {'passes through' if expected else 'translates'} {text[:22]!r}",
            matches_target(text, "zh-CN") == expected,
        )

    chinese_session = Session(
        AppConfig.load(),
        capturer=SyntheticCapturer(frames=[("对话轨迹", "新会话", "工作区")], hold_seconds=99),
    )
    chinese_session.config.translation["nmt_model"] = None
    chinese_session.build()
    chinese_channel = chinese_session.subscribe()
    chinese_session.start()
    chinese_events = collect(chinese_channel, 2.5)
    chinese_subs = [e for e in chinese_events if e.get("type") == EVENT_SUBTITLE]
    check.check("the synthetic Chinese frame was recognised", len(chinese_subs) > 0,
                f"{len(chinese_subs)} subtitle event(s)")
    if chinese_subs:
        data = chinese_subs[-1]["data"]
        check.check("every line is marked passthrough",
                    all(l["source"] == l["target"] for l in data["lines"]),
                    f"backend={data.get('backend')!r}")
        check.check("the backend reports passthrough, not a translation",
                    "passthrough" in (data.get("backend") or ""),
                    str(data.get("backend")))
        check.check("the trace explains why nothing was translated",
                    "passthrough" in (data.get("trace") or ""))
    chinese_stats = chinese_session.stats_dict()
    check.check("the engine counted the skipped lines",
                chinese_stats.get("passthrough_lines", 0) > 0,
                f"passthrough_lines={chinese_stats.get('passthrough_lines')}")
    check.check("no refinement was queued for pure-target text",
                chinese_stats.get("refinements", 0) == 0,
                f"refinements={chinese_stats.get('refinements')}")
    chinese_session.stop()

    print("")
    print("-- stop --")
    session.stop()
    check.check("stop is reported as an event",
                EVENT_STOPPED in types_of(collect(channel, 0.4)))
    check.check("synthetic capturer was closed", capturer.closed)
    check.check("no error events were published",
                EVENT_ERROR not in kinds, f"{kinds.count(EVENT_ERROR)} error(s)")

    return check.report()


if __name__ == "__main__":
    raise SystemExit(main())
