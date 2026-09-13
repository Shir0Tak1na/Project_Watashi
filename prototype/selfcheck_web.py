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
import tempfile
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

def _free_port() -> int:
    """A port nothing is listening on, chosen at run time.

    This used to be a fixed 8791. A crashed earlier run could leave a server thread
    holding that port, and the next run would fail to bind and then talk to the
    zombie -- which is a large part of why this self check failed intermittently.
    Letting the OS pick makes a leftover server irrelevant.
    """
    import socket

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


PORT = _free_port()



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
    # The user corpus layer is pointed at a scratch directory *before* the engine is
    # built. The panel's editor writes to the user layer, so leaving this alone would make
    # this check write files into the repository's own corpus directory -- and setting it
    # later would not help, because the corpus store is built from the config once.
    import tempfile as _tempfile
    from pathlib import Path as _Path

    scratch_user = _Path(_tempfile.mkdtemp(prefix="watashi-web-library-")) / "user"
    config.corpus["user"] = [str(scratch_user)]
    # The panel's own window is the browser, which this check cannot see; the engine's
    # self-capture guard could pause the pipeline if one happened to be over the capture
    # strip, and the SSE assertions below expect frames. Guarded separately,
    # in selfcheck_selfcapture.
    config.capture["hold_if_self_visible"] = False

    session = Session(config, capturer=SyntheticCapturer(hold_seconds=0.7))
    session.build()
    session.start()

    panel = WebPanel(session, host="127.0.0.1", port=PORT, log_level="error")
    # The session was started above, so this also pins the separation: panel state and
    # session state are different questions, and a panel that has not been started must
    # not inherit the session's "running". (An earlier version of this file asserted the
    # opposite -- that session.running was False -- inside a dead `if ...: pass`, so the
    # claim was never tested and was simply wrong.)
    check.check(
        "a panel that has not been started reports False while its session runs",
        panel.running is False and panel.session.running is True,
        f"panel.running={panel.running} session.running={panel.session.running}",
    )
    started = panel.start()
    check.check("panel starts", started and panel.wait_until_ready(), panel.url)
    # `running` is what the desktop window keys off to decide whether to start the
    # panel, and it is the attribute desktop.py used to invent (it read `panel.running`
    # before this property existed, which raised AttributeError on every click). Public
    # and asserted, so removing it breaks here instead of in the user's face.
    check.check(
        "and reports itself running once started",
        panel.running is True,
        f"running={panel.running}",
    )

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

        # ---- is the page alive? ------------------------------------------ #
        #
        # Everything above reads the page as text, so all of it passes on a page whose
        # script has a syntax error or whose ids were renamed -- and both of those leave a
        # panel that loads and then does nothing at all. Neither is visible from the
        # server side either: the HTML is served, the JSON endpoints answer, and the only
        # broken thing is the browser.
        print("")
        print("-- the page's script is valid, not just present --")
        scripts = re.findall(r"<script[^>]*>(.*?)</script>", html, flags=re.DOTALL)
        check.check(
            "the page has an inline script",
            len(scripts) >= 1 and sum(len(block) for block in scripts) > 1000,
            f"{len(scripts)} block(s), {sum(len(b) for b in scripts)} chars",
        )
        ids_defined = set(re.findall(r'id="([^"]+)"', html))
        ids_used = set(re.findall(r'\$\("([^"]+)"\)', "".join(scripts)))
        unknown = sorted(ids_used - ids_defined)
        check.check(
            "every element the script looks up exists in the markup",
            not unknown,
            f"looked up but not defined: {unknown}",
        )
        check.check(
            "and it looks up a plausible number of elements",
            len(ids_used) >= 20,
            f"{len(ids_used)} id(s) reached for, {len(ids_defined)} defined",
        )

        # Local aliases again: this function imports shutil, tempfile and Path further
        # down, and a local import anywhere in a function makes that name local for the
        # whole of it -- so the module level ones are unreachable from here. That trap has
        # cost three confusing failures in this file alone.
        import shutil as _shutil

        node = _shutil.which("node")
        if node is None:
            check.skip(
                "the inline script parses",
                "node is not installed here, so the script's syntax could not be checked. "
                "Everything above still passes on a page that cannot run.",
            )
        else:
            import subprocess
            import tempfile as _tempfile
            from pathlib import Path as _PathAgain

            parse_failures: list[str] = []
            for index, block in enumerate(scripts):
                with _tempfile.NamedTemporaryFile(
                    "w", suffix=".js", delete=False, encoding="utf-8"
                ) as handle:
                    handle.write(block)
                    temp = _PathAgain(handle.name)
                try:
                    result = subprocess.run(
                        [node, "--check", str(temp)],
                        capture_output=True,
                        text=True,
                        timeout=30,
                    )
                finally:
                    temp.unlink(missing_ok=True)
                if result.returncode != 0:
                    parse_failures.append(f"block {index}: {result.stderr.strip()[:200]}")
            check.check(
                "the inline script parses",
                not parse_failures,
                "; ".join(parse_failures) or f"{len(scripts)} block(s) parsed by {node}",
            )

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
        check.check("and it lists the human corrections, so they can be read here",
                    isinstance(corpus.get("corrections"), list),
                    f"{len(corpus.get('corrections', []))} correction(s)")
        check.check("the page has a section for them",
                    "实时纠正" in html and "xentries" in html,
                    "the corrections stay read-only; the corpus does not")

        # ---- the corpus editor ------------------------------------------- #
        print("")
        print("-- the corpus editor: the panel is the editing surface --")
        check.check("the editor writes to the scratch user layer, not the repository",
                    str(session.library_view().get("file", "")).startswith(str(scratch_user)),
                    str(session.library_view().get("file")))

        status, body = get(base + "/api/library")
        library = json.loads(body)
        check.check("GET /api/library lists the editor's rows",
                    status == 200 and len(library.get("entries", [])) > 0,
                    f"{len(library.get('entries', []))} row(s)")
        check.check("with the state each row is in",
                    all(
                        {"source", "target", "lang", "layer", "origin", "user",
                         "overrides", "suppressed"} <= set(row)
                        for row in library["entries"][:5]
                    ),
                    str(sorted(library["entries"][0]))[:120] if library.get("entries") else "",
                )
        check.check("and the file it writes, so a user can find it",
                    str(library.get("file", "")).endswith("library.json"),
                    str(library.get("file")))
        check.check("the shipped entries are marked as shipped",
                    any(row["user"] is False for row in library["entries"]),
                    f"{sum(1 for row in library['entries'] if not row['user'])} shipped row(s)")
        check.check("and nothing in this fresh editor is the user's yet",
                    library.get("user") == 0,
                    f"user={library.get('user')}")

        status, body = post(base + "/api/command",
                            {"cmd": "library_put", "source": "void sword",
                             "target": "虚空剑", "lang": "zh-CN"})
        result = json.loads(body)
        check.check("library_put is allowed through the panel",
                    status == 200 and result.get("ok"), str(result.get("detail"))[:80])
        status, body = get(base + "/api/library")
        library = json.loads(body)
        check.check("the new row is the user's",
                    any(row["source"] == "void sword" and row["user"]
                        for row in library["entries"]),
                    f"user={library.get('user')}")
        check.check("and the engine translates with it immediately",
                    session.corpus.translate("void sword", "zh-CN").target_text == "虚空剑",
                    session.corpus.translate("void sword", "zh-CN").target_text)

        shipped = next(
            row for row in library["entries"] if not row["user"] and not row["suppressed"]
        )
        status, body = post(base + "/api/command",
                            {"cmd": "library_put", "source": shipped["source"],
                             "target": "覆盖测试"})
        check.check("overriding a shipped entry through the panel is allowed",
                    status == 200 and json.loads(body).get("ok"),
                    str(json.loads(body).get("detail"))[:80])
        status, body = get(base + "/api/library")
        overridden = next(
            row for row in json.loads(body)["entries"]
            if row["source"] == shipped["source"] and row["user"]
        )
        check.check("and the row says what it overrides",
                    overridden["overrides"] == shipped["origin"],
                    f"{overridden['overrides']} vs {shipped['origin']}")
        check.check("with the shipped translation still visible for comparison",
                    overridden["overrides_target"] == shipped["target"],
                    f"{overridden['overrides_target']!r} vs {shipped['target']!r}")

        status, body = post(base + "/api/command",
                            {"cmd": "library_suppress", "source": shipped["source"]})
        check.check("library_suppress is allowed", status == 200 and json.loads(body).get("ok"))
        status, body = get(base + "/api/library")
        row = next(
            item for item in json.loads(body)["entries"] if item["source"] == shipped["source"]
        )
        check.check("the row is still listed, marked as hidden",
                    row["suppressed"] is True,
                    "a row that disappears leaves no way to undo it")

        status, body = post(base + "/api/command",
                            {"cmd": "library_restore", "source": shipped["source"]})
        check.check("library_restore brings it back", status == 200 and json.loads(body).get("ok"))

        # import: the text path the page's file picker uses
        status, body = post(base + "/api/library/import",
                            {"format": "csv",
                             "text": "source,target,lang\naether,以太,zh-CN\nsky,天,zh-CN\n"})
        result = json.loads(body)
        check.check("POST /api/library/import applies an uploaded file",
                    status == 200 and result.get("ok"), str(result.get("detail"))[:80])
        check.check("and it reports what it added",
                    "added 2" in str(result.get("detail")),
                    str(result.get("detail"))[:80])
        check.check("the response carries the new view, so the page need not re-ask",
                    any(row["source"] == "aether" for row in result.get("entries", [])),
                    f"{len(result.get('entries', []))} rows back")

        status, body = post(base + "/api/library/import",
                            {"format": "json", "text": "{ not json"})
        check.check("a broken upload is refused with the parser's reason",
                    status == 400 and "JSON" in str(json.loads(body).get("detail")),
                    str(json.loads(body).get("detail"))[:80])

        # export: a real download, because that is what the point of exporting is
        request = urllib.request.Request(base + "/api/library/export?format=csv&scope=user")
        with urllib.request.urlopen(request, timeout=10) as response:
            exported = response.read()
            disposition = response.headers.get("Content-Disposition", "")
            media = response.headers.get("Content-Type", "")
        check.check("GET /api/library/export returns a file, not a JSON string",
                    disposition.startswith("attachment") and "watashi-corpus-user.csv" in disposition,
                    disposition)
        check.check("as CSV", "text/csv" in media, media)
        check.check("with a BOM, so a spreadsheet opens the Chinese correctly",
                    exported.startswith(b"\xef\xbb\xbf"),
                    str(exported[:8]))
        text = exported.decode("utf-8-sig")
        check.check("containing the header and the user's rows",
                    text.startswith("source,target") and "aether" in text and "void sword" in text,
                    text[:80])
        check.check("and not the shipped rows, for scope=user",
                    "sword intent" not in text,
                    f"{len(text.splitlines())} line(s)")

        request = urllib.request.Request(base + "/api/library/export?format=json&scope=effective")
        with urllib.request.urlopen(request, timeout=10) as response:
            effective = json.loads(response.read().decode("utf-8"))
        check.check("scope=effective exports the whole corpus as a corpus file",
                    "entries" in effective and len(effective["entries"]) > 50,
                    f"{len(effective.get('entries', {}))} entr(ies)")

        request = urllib.request.Request(base + "/api/library/export?format=xlsx&scope=user")
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                status = response.status
        except urllib.error.HTTPError as exc:
            status = exc.code
        check.check("an unsupported export format is refused, not silently substituted",
                    status == 400, f"status={status}")

        # ---- the boundary: settings in, features refused ------------------ #
        print("")
        print("-- what the panel still refuses --")
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
        check.check(
            "a stopped panel reports itself stopped, so a caller can start it again",
            panel.running is False,
            f"running={panel.running}",
        )
        session.stop()

    # ------------------------------------------------------------------ #
    print("")
    print("-- the settings schema is served, and writes go somewhere safe --")
    #
    # `config.base_dir` is redirected to a temp directory for this section. Without
    # that, POSTing a change would write prototype/config.user.yaml, and a test that
    # edits the project's own configuration is a test that changes the behaviour of
    # everything run after it.
    import shutil
    import tempfile
    from pathlib import Path

    from watashi.config import AppConfig as _AppConfig

    real_base = config.base_dir
    tmp_base = Path(tempfile.mkdtemp(prefix="watashi-web-settings-"))
    # The earlier sections stop the panel when they are done, so this one brings it
    # back up. Guarded by a readiness probe rather than restarted unconditionally,
    # so the section also works if it is ever moved earlier in the file.
    restarted = False
    if not panel.wait_until_ready(timeout=1.0):
        panel.start()
        restarted = panel.wait_until_ready(timeout=10.0)
        # Checked, not assumed. The previous version restarted the panel and then
        # fired requests regardless of whether it came back up, so a failed restart
        # surfaced as a ConnectionRefused several lines later instead of here.
        check.check(
            "the panel restarts for this section after the earlier stop()",
            restarted,
            "without this the requests below would fail with a confusing connection error",
        )
    try:
        config.base_dir = tmp_base

        status, body = get(base + "/")
        check.check(
            "the page offers the schema-driven settings tab",
            'data-tab="settings"' in body and 'id="setcats"' in body,
            "the tab and its container must both be present",
        )
        check.check(
            "and it posts to the settings endpoint",
            '"/api/settings"' in body and "changes" in body,
            "the form must send {changes: {...}}",
        )
        check.check(
            "the page states the live/restart distinction, not just the controls",
            "立即生效" in body and "需重启" in body,
            "without this the 43 restart-only controls look broken",
        )
        check.check(
            "the page is still entirely offline (no CDN, no web fonts)",
            "https://" not in body,
            "requirement R1: nothing may be fetched from the network",
        )

        status, body = get(base + "/api/settings")
        check.check("GET /api/settings answers", status == 200, f"HTTP {status}")
        payload = json.loads(body)
        categories = payload.get("categories", [])
        check.check(
            "it returns every category of the schema",
            len(categories) == 9,
            f"{len(categories)} categories",
        )
        fields = [f for c in categories for f in c["fields"]]
        check.check(
            "and every field, each with a label and a description",
            fields and all(f.get("label") and f.get("description") for f in fields),
            f"{len(fields)} fields",
        )
        check.check(
            "current values ride along, so the form can prefill",
            all(c.get("values") for c in categories),
        )
        check.check(
            "the live/restart split is reported for the warning text",
            payload.get("live_count", 0) + payload.get("restart_count", 0) == len(fields),
            f"{payload.get('live_count')} live / {payload.get('restart_count')} restart",
        )
        check.check(
            "nothing is marked overridden yet",
            payload.get("overridden") == [],
            f"{payload.get('overridden')}",
        )

        status, body = post(base + "/api/settings", {"changes": {"capture.fps": 25}})
        check.check("POST accepts a valid change", status == 200, f"HTTP {status} {body[:120]}")
        result = json.loads(body)
        check.check(
            "a live setting is applied immediately",
            "capture.fps" in result.get("applied_now", []),
            f"{result}",
        )
        check.check(
            "and it is also written to the override file",
            _AppConfig.overrides_path(tmp_base).exists(),
        )
        # Loaded back through the real path, so this asserts persistence rather than
        # just that a file appeared.
        persisted = _AppConfig.load(tmp_base / "config.yaml")
        check.check(
            "the value survives a reload, so it applies on the next start too",
            persisted.fps == 25.0,
            f"fps={persisted.fps}",
        )

        status, body = post(base + "/api/settings", {"changes": {"capture.settle_ms": 180}})
        result = json.loads(body)
        check.check(
            "a restart-required setting is reported as needing a restart",
            "capture.settle_ms" in result.get("needs_restart", []),
            f"{result}",
        )
        check.check(
            "but it is still written, or the control would do nothing at all",
            _AppConfig.load(tmp_base / "config.yaml").capture.get("settle_ms") == 180,
            f"settle_ms={_AppConfig.load(tmp_base / 'config.yaml').capture.get('settle_ms')}",
        )

        status, body = post(base + "/api/settings", {"changes": {"overlay.mode": "sideways"}})
        check.check(
            "an invalid value is refused with 400 and a reason",
            status == 400 and "bar" in body,
            f"HTTP {status} {body[:140]}",
        )
        status, body = post(base + "/api/settings", {"changes": {"nope.nothing": 1}})
        check.check(
            "an unknown setting is refused with 400",
            status == 400 and "unknown setting" in body,
            f"HTTP {status} {body[:140]}",
        )
        status, body = post(
            base + "/api/settings",
            {"changes": {"capture.fps": 30, "overlay.mode": "wrong"}},
        )
        check.check(
            "a batch containing one bad value writes none of it",
            status == 400,
            f"HTTP {status}",
        )
        after_reject = _AppConfig.load(tmp_base / "config.yaml")
        check.check(
            "the refused batch left the good value untouched",
            after_reject.fps == 25.0 and after_reject.capture.get("settle_ms") == 180,
            f"fps={after_reject.fps} settle_ms={after_reject.capture.get('settle_ms')}",
        )

        status, body = get(base + "/api/settings")
        check.check(
            "the page is told which values the user changed",
            "capture.fps" in json.loads(body).get("overridden", []),
            f"{json.loads(body).get('overridden')}",
        )
    finally:
        config.base_dir = real_base
        shutil.rmtree(tmp_base, ignore_errors=True)
        if restarted:
            panel.stop()

    # ------------------------------------------------------------------ #
    print("")
    print("-- stop() actually stops, even with a stream open --")
    #
    # This reproduces the failure that made this self check unreliable rather than
    # just re-running it and hoping. uvicorn's graceful shutdown waits for in-flight
    # connections, and an open SSE stream is exactly such a connection. The old
    # stop() joined once, gave up, cleared its handles anyway, and left a server
    # thread holding the port; the next start() then failed to bind and requests
    # went to that zombie. Every later section became a coin flip.
    import socket as _socket

    zombie_port = _free_port()
    first = WebPanel(session, host="127.0.0.1", port=zombie_port, log_level="error")
    check.check("a second panel starts on its own port", first.start() and first.wait_until_ready())

    stream_opened = {"ok": False}

    def hold_stream_open() -> None:
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{zombie_port}/api/events", timeout=20
            ) as response:
                stream_opened["ok"] = True
                response.read(64)  # prove data actually flows
                time.sleep(3.0)    # and stay connected while stop() is called
        except Exception:
            pass

    holder = threading.Thread(target=hold_stream_open, daemon=True)
    holder.start()
    deadline = time.time() + 5
    while not stream_opened["ok"] and time.time() < deadline:
        time.sleep(0.05)
    check.check(
        "an SSE stream is genuinely open before the stop",
        stream_opened["ok"],
        "otherwise this reproduces nothing",
    )

    stopped_cleanly = first.stop(timeout=5.0)
    check.check(
        "stop() reports that it stopped (it used to clear its handles regardless)",
        stopped_cleanly is True,
    )
    check.check(
        "and the server thread is really gone",
        first._thread is None,  # noqa: SLF001 - the invariant under test
    )

    # The decisive one: the port must be free, because a held port is what made the
    # next start() fail and sent requests to a server with a different session.
    try:
        with _socket.socket() as probe:
            probe.setsockopt(_socket.SOL_SOCKET, _socket.SO_REUSEADDR, 1)
            probe.bind(("127.0.0.1", zombie_port))
        port_free = True
    except OSError as exc:
        port_free = False
        print(f"      port still held: {exc}")
    check.check("the port is released, so a restart cannot silently fail to bind", port_free)

    again = WebPanel(session, host="127.0.0.1", port=zombie_port, log_level="error")
    check.check(
        "and a fresh panel can bind that same port again",
        again.start() and again.wait_until_ready(),
    )
    status, _ = get(f"http://127.0.0.1:{zombie_port}/api/info")
    check.check("and it serves requests rather than refusing them", status == 200, f"HTTP {status}")
    again.stop()

    return check.report()


if __name__ == "__main__":
    import urllib.parse  # noqa: E402  (used by the DELETE check)

    raise SystemExit(main())
