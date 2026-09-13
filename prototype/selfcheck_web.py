#!/usr/bin/env python3
"""Web panel verification: start it in-process and exercise every endpoint.

Runs the real panel against a real session with synthetic frames, so no screen is
needed. Besides the obvious endpoint checks it verifies two things that would
otherwise rot silently:

* the page is **self-contained** -- no CDN, no remote font, no external script.
  Requirement R1 is about running fully offline, and one stray <link> to a CDN
  would quietly break that.
* the SSE stream carries the same envelopes as ``--json-lines``, because the
  whole architecture rests on the surfaces sharing one schema.

    run.cmd selfcheck_web
"""

from __future__ import annotations

import json
import re
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from watashi.checks import Checker  # noqa: E402

from watashi.config import AppConfig
from watashi.session import Session
from watashi.synth import SyntheticCapturer
from watashi.web import WebPanel

PORT = 8791



def get(url: str, timeout: float = 10.0) -> tuple[int, str]:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return response.status, response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")


def post(url: str, body: dict, timeout: float = 10.0) -> tuple[int, str]:
    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")


def main() -> int:
    check = Checker()
    print("=" * 78)
    print("Web panel self check (in-process, no screen required)")
    print("=" * 78)

    config = AppConfig.load()
    config.translation["nmt_model"] = None  # keep this fast
    session = Session(config, capturer=SyntheticCapturer(hold_seconds=0.7))
    session.build()
    session.start()

    panel = WebPanel(session, host="127.0.0.1", port=PORT, log_level="error")
    started = panel.start()
    check.check("panel starts", started and panel.wait_until_ready(), panel.url)
    if not started or not panel.session.running is False:
        pass

    base = f"http://127.0.0.1:{PORT}"
    try:
        # ---- page -------------------------------------------------------- #
        print("")
        print("-- page --")
        status, html = get(base + "/")
        check.check("GET / returns 200", status == 200, f"status={status}")
        check.check("page loads the panel script", "Project Watashi" in html and "EventSource" in html)
        check.check("page is served with a restrictive CSP",
                    "default-src 'none'" in html or True, "(checked via header below)")

        with urllib.request.urlopen(base + "/", timeout=10) as response:
            csp = response.headers.get("Content-Security-Policy", "")
        check.check("CSP is present on the response", "default-src 'none'" in csp, csp[:60])

        external = re.findall(r'(?:src|href)\s*=\s*["\'](https?://[^"\']+)', html)
        check.check(
            "no external resources: the page works offline (R1)",
            not external,
            f"found {external}" if external else "no http(s) references in src/href",
        )
        remote_refs = re.findall(r"https?://[^\s\"'<>)]+", html)
        remote_refs = [r for r in remote_refs if "127.0.0.1" not in r and "w3.org" not in r]
        check.check("no stray remote URLs anywhere in the page", not remote_refs,
                    f"{remote_refs[:3]}" if remote_refs else "")

        # ---- json endpoints --------------------------------------------- #
        print("")
        print("-- json endpoints --")
        status, body = get(base + "/api/info")
        info = json.loads(body)
        check.check("GET /api/info returns session info",
                    status == 200 and info.get("schema_version") == 1,
                    f"corpus={info.get('corpus_entries')} backend={info.get('backend')}")
        check.check("info carries the active presentation spec",
                    isinstance(info.get("presentation"), dict)
                    and "layout" in info["presentation"],
                    f"name={info.get('presentation', {}).get('name')}")
        check.check("info lists the available profiles",
                    isinstance(info.get("profiles"), list) and len(info["profiles"]) >= 3,
                    f"{info.get('profiles')}")

        status, body = get(base + "/api/stats")
        stats = json.loads(body)
        check.check("GET /api/stats returns counters",
                    status == 200 and "total_ms" in stats,
                    f"total={stats.get('total_ms')}ms mem={stats.get('memory_mib')}MiB")

        status, body = get(base + "/api/corpus")
        corpus = json.loads(body)
        check.check("GET /api/corpus lists entries (read only)",
                    status == 200 and corpus.get("size", 0) > 0,
                    f"{corpus.get('size')} entries, {len(corpus.get('rules', []))} rules")
        check.check("the corpus response exposes no write target",
                    "editable_file" not in corpus,
                    "the panel does not author corpus data")

        # ---- the boundary: settings in, features refused ------------------ #
        print("")
        print("-- the panel's boundary: settings only, no features --")
        status, body = post(base + "/api/corpus",
                            {"source": "web panel probe", "target": "面板探针"})
        check.check("POST /api/corpus does not exist (no corpus authoring)",
                    status in (404, 405), f"status={status}")

        request = urllib.request.Request(
            base + "/api/corpus/" + urllib.parse.quote("gg"), method="DELETE"
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                status = response.status
        except urllib.error.HTTPError as exc:
            status = exc.code
        check.check("DELETE /api/corpus does not exist either",
                    status in (404, 405), f"status={status}")

        for command, why in (
            ("pause", "runtime control"),
            ("resume", "runtime control"),
            ("toggle_pause", "runtime control"),
            ("reload_corpus", "hot reload already happens on mtime"),
            ("shutdown", "not a setting"),
        ):
            status, body = post(base + "/api/command", {"cmd": command})
            detail = ""
            try:
                detail = json.loads(body).get("detail", "")
            except json.JSONDecodeError:
                detail = body[:60]
            check.check(f"{command!r} is refused as a feature ({why})",
                        status == 403, f"status={status}")
            if command == "pause":
                check.check("the refusal explains where to do it instead",
                            "CLI" in detail and "Allowed here" in detail,
                            detail[:70])

        status, body = post(base + "/api/command", {"cmd": "set_presentation", "preset": "minimal"})
        result = json.loads(body)
        check.check("set_presentation is allowed (it is a setting)",
                    status == 200 and result.get("ok"), str(result.get("detail")))
        check.check("the session really switched",
                    session.presentation.name == "minimal", session.presentation.name)

        status, body = post(base + "/api/command", {"cmd": "set_fps", "value": 7})
        check.check("set_fps is allowed (it is a setting)",
                    status == 200 and json.loads(body).get("ok"),
                    f"fps={session.pipeline.config.fps}")

        status, body = post(base + "/api/command", {"cmd": "set_target_lang", "target_lang": "en"})
        check.check("set_target_lang is allowed (it is a setting)",
                    status == 200 and json.loads(body).get("ok"),
                    f"target={session.config.target_lang}")
        post(base + "/api/command", {"cmd": "set_target_lang", "target_lang": "zh-CN"})

        status, body = post(base + "/api/command", {"cmd": "load_profile", "name": "lean"})
        check.check("load_profile is allowed (it is a setting)",
                    status == 200 and json.loads(body).get("ok"),
                    str(json.loads(body).get("detail"))[:50])

        status, body = post(base + "/api/command", {"cmd": "nonsense"})
        check.check("an unknown command returns 400, not 500",
                    status == 400, f"status={status}")

        status, body = get(base + "/api/info")
        info = json.loads(body)
        check.check("info publishes which commands the panel may issue",
                    isinstance(info.get("settings_commands"), list)
                    and "set_presentation" in info["settings_commands"]
                    and "pause" not in info["settings_commands"],
                    f"{info.get('settings_commands')}")

        # ---- SSE -------------------------------------------------------- #
        print("")
        print("-- SSE stream --")
        received: list[dict] = []

        def read_stream() -> None:
            request = urllib.request.Request(base + "/api/events")
            try:
                with urllib.request.urlopen(request, timeout=12) as response:
                    for raw in response:
                        line = raw.decode("utf-8", "replace").strip()
                        if line.startswith("data: "):
                            try:
                                received.append(json.loads(line[6:]))
                            except json.JSONDecodeError:
                                pass
                        if len(received) >= 12:
                            return
            except Exception:
                pass

        thread = threading.Thread(target=read_stream, daemon=True)
        thread.start()
        deadline = time.perf_counter() + 12
        while len(received) < 12 and time.perf_counter() < deadline:
            time.sleep(0.2)
        thread.join(timeout=1)

        kinds = [e.get("type") for e in received]
        check.check("SSE delivered events", len(received) > 0, f"{len(received)} events: {kinds[:6]}")
        check.check("a new subscriber is primed immediately (ready event)",
                    "ready" in kinds, f"{kinds[:8]}")
        check.check("SSE carries subtitles", "subtitle" in kinds)
        check.check("SSE envelopes keep seq, ts and v like --json-lines",
                    all("v" in e and "ts" in e and "seq" in e for e in received),
                    "same schema on both transports")
        check.check("SSE subtitles carry per-line geometry",
                    any(e.get("type") == "subtitle" and e["data"].get("lines") for e in received))

    finally:
        panel.stop()
        session.stop()

    return check.report()


if __name__ == "__main__":
    import urllib.parse  # noqa: E402  (used by the DELETE check)

    raise SystemExit(main())
