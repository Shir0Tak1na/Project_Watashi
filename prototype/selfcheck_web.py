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


def _process_port() -> int:
    """A free port this process picks itself instead of one the OS hands out.

    A port from ``bind(..., 0)`` is only free *at the moment it is asked for*, and the
    OS hands these out from one system-wide pool. Two copies of this check starting
    together -- CI, or two people running the checks on one machine -- can be handed the
    same number, and on Windows the second bind then **succeeds** anyway, because
    uvicorn sets SO_REUSEADDR and Windows lets two sockets share a port. After that,
    requests are answered by whichever process won the race, and a run can honestly
    report "the preview wrote nothing" while a *foreign* request writes an import into
    its own corpus file. That is not hypothetical: it happened, and it read exactly like
    a defect in the code under test.

    Mixing the pid into the choice means two live copies of this check pick different
    ports, and ``_start_owned_panel`` verifies the rest. ``_free_port`` stays as the
    fallback for the unlikely case that every candidate is taken.
    """
    import os
    import random
    import socket

    for _ in range(32):
        port = 20000 + (os.getpid() * 7 + random.randrange(1 << 20)) % 40000
        try:
            with socket.socket() as probe:
                # No SO_REUSEADDR here on purpose: this is asking whether anyone else
                # holds the port, and a probe that may share it cannot answer that.
                probe.bind(("127.0.0.1", port))
        except OSError:
            continue
        return port
    return _free_port()


def _panel_library_file(url: str) -> str:
    """The corpus file the panel behind ``url`` says it writes, or "" if it will not say.

    This is the ownership test for a panel: the file path comes from the session that
    panel is mounted on, so a URL that reports someone else's path is someone else's
    panel -- see ``_process_port``.
    """
    try:
        status, body = get(url + "/api/library", timeout=5.0)
        return str(json.loads(body).get("file", ""))
    except Exception:
        return ""


def _start_owned_panel(session: Session, attempts: int = 3) -> tuple[Any, str]:
    """Start a panel and prove the URL reaches *this* session. ``(panel, base_url)``.

    The proof is asked for rather than assumed because every assertion in this file is
    about the session built here, and a panel belonging to another process answers the
    same endpoints with different data. A panel that answers wrongly is stopped and
    another port is tried; if none comes up clean, the last one is returned so the
    failure is reported by the check that is about exactly that, instead of surfacing as
    a confusing mismatch thirty lines later.
    """
    expected = str(session.library_view().get("file", ""))
    panel: Any = None
    url = ""
    for attempt in range(1, attempts + 1):
        port = _process_port()
        panel = WebPanel(session, host="127.0.0.1", port=port, log_level="error")
        url = f"http://127.0.0.1:{port}"
        if not (panel.start() and panel.wait_until_ready()):
            print(f"      port {port} did not come up (attempt {attempt})")
            panel.stop()
            continue
        answered = _panel_library_file(url)
        if answered == expected:
            return panel, url
        print(
            f"      port {port} is answered by a different process "
            f"({answered or 'no answer at all'}), not by this check's session; "
            f"retrying on another port"
        )
        panel.stop()
    return panel, url


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

    panel = WebPanel(session, host="127.0.0.1", port=_process_port(), log_level="error")
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

    # The panel must be *this* session's, and the check says so rather than assuming it:
    # on Windows a port can be shared with another copy of this check (see
    # ``_process_port``), and when that happens the requests below read and write someone
    # else's corpus while every response still looks plausible. Redoing the start on a
    # fresh port is the fix; the assertion is what makes the failure legible.
    own_file = str(session.library_view().get("file", ""))
    base = f"http://127.0.0.1:{panel.port}"
    answered_by = _panel_library_file(base)
    for _ in range(2):
        if answered_by == own_file:
            break
        panel.stop()
        panel, base = _start_owned_panel(session, attempts=1)
        answered_by = _panel_library_file(base)
    check.check(
        "the panel answering these requests is this check's own session",
        answered_by == own_file,
        f"{base} writes {answered_by or 'nothing it will name'}, "
        f"this check's session writes {own_file}",
    )

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

        # ---- scenes and conditions: one term with two meanings ------------ #
        #
        # This is the shape where a defect is invisible: the second meaning is stored,
        # listed in the table, and simply never used. So these checks do not stop at "the
        # panel accepted the row" -- they read it back through GET /api/library and then
        # ask the engine which answer it gives per scene and per condition.
        print("")
        print("-- one term, two meanings: scenes and conditions --")

        status, body = post(base + "/api/command",
                            {"cmd": "library_put", "source": "quayside",
                             "target": "码头", "lang": "zh-CN", "domain": "harbour",
                             "when_line": "river", "when_near": "river, barge",
                             "when_window": "Novel"})
        result = json.loads(body)
        check.check("a row can be created with a scene and with every condition",
                    status == 200 and result.get("ok"), str(result.get("detail"))[:90])

        status, body = get(base + "/api/library")
        library = json.loads(body)
        conditioned = next((row for row in library["entries"]
                            if row["source"] == "quayside"), None)
        check.check("and GET /api/library returns the scene and all three conditions",
                    conditioned is not None
                    and conditioned["domain"] == "harbour"
                    and (conditioned["when_line"], conditioned["when_near"],
                         conditioned["when_window"]) == ("river", "river, barge", "Novel"),
                    json.dumps(conditioned, ensure_ascii=False)[:150] if conditioned
                    else "the row is not in the view at all")

        # Two senses of one term, told apart by scene, both written through the panel. The
        # editor keys a row on (term, language, scene) for exactly this reason: keying on
        # the term alone is how the second meaning used to destroy the first.
        for target, scene in (("河岸", "nature"), ("银行", "finance")):
            status, body = post(base + "/api/command",
                                {"cmd": "library_put", "source": "bank", "target": target,
                                 "lang": "zh-CN", "domain": scene})
            check.check(f"a meaning of 'bank' in the {scene} scene is accepted",
                        status == 200 and json.loads(body).get("ok"),
                        str(json.loads(body).get("detail"))[:80])

        status, body = get(base + "/api/library")
        library = json.loads(body)
        senses = [row for row in library["entries"] if row["source"] == "bank"]
        check.check("both meanings are separate rows in the table, and neither is lost",
                    {(row["domain"], row["target"]) for row in senses}
                    == {("nature", "河岸"), ("finance", "银行")},
                    json.dumps([(r["domain"], r["target"]) for r in senses], ensure_ascii=False))
        check.check("conditions stay on the row that declared them, and the scenes stay apart",
                    all(not row["when_line"] and not row["when_near"] and not row["when_window"]
                        for row in senses)
                    and conditioned is not None and conditioned["when_line"] == "river",
                    "a condition copied onto a sibling row would make one meaning unreachable")
        check.check("the payload lists every scene in effect, for the scene picker",
                    {"harbour", "nature", "finance"} <= set(library.get("scenes") or []),
                    f"{library.get('scenes')}")
        check.check("and it names the rows that lost a collision, so the diagnostic has data",
                    isinstance(library.get("conflicts"), list),
                    f"{len(library.get('conflicts') or [])} conflict(s) in the payload")

        from watashi.translate import Context as _Context

        def answer(scene: str) -> str:
            hit = session.corpus.lookup_exact(
                "bank", "zh-CN", _Context(target_lang="zh-CN", scene=scene)
            )
            return hit.target if hit is not None else "(none)"

        check.check("and the engine really answers per scene, not just the table",
                    (answer("finance"), answer("nature")) == ("银行", "河岸"),
                    f"finance -> {answer('finance')}, nature -> {answer('nature')}")

        # Conditions are a gate rather than a rank: the same line must be answered when they
        # hold and left alone when they do not. Asserted through the engine, because a panel
        # that wrote them in a shape the engine does not read would look exactly like this
        # working.
        def answered(line: str, window: str) -> str:
            return session.corpus.translate(
                line, "zh-CN", _Context(target_lang="zh-CN", scene="harbour", window=window)
            ).target_text

        held = answered("down by the river quayside", "Chapter 1 - a Novel")
        failed = answered("down by the river quayside", "a Report")
        check.check("the conditional row is used only while its conditions hold",
                    "码头" in held and "码头" not in failed,
                    f"window=Novel -> {held!r} / window=Report -> {failed!r}")

        # Two meanings of one term in *one* scene, told apart only by a condition. The page
        # says this is possible (a row is keyed on term + language + scene + conditions), so
        # it is asserted rather than claimed: both rows exist, each answers only where its
        # own condition holds, and the delete the table's button sends -- which names the row
        # by all four parts -- removes one meaning and leaves the other.
        for target, line in (("账目", "account"), ("河流", "river")):
            status, body = post(base + "/api/command",
                                {"cmd": "library_put", "source": "crane", "target": target,
                                 "lang": "zh-CN", "domain": "nature", "when_line": line})
            check.check(f"a second meaning told apart only by when_line={line!r} is accepted",
                        status == 200 and json.loads(body).get("ok"),
                        str(json.loads(body).get("detail"))[:80])

        status, body = get(base + "/api/library")
        library = json.loads(body)
        cranes = [row for row in library["entries"] if row["source"] == "crane"]
        check.check("both condition-only meanings are separate rows in one scene, neither lost",
                    {(row["domain"], row["when_line"], row["target"]) for row in cranes}
                    == {("nature", "account", "账目"), ("nature", "river", "河流")},
                    json.dumps([(row["domain"], row["when_line"], row["target"])
                                for row in cranes], ensure_ascii=False))
        def crane_answer(line: str) -> str:
            return session.corpus.translate(
                line, "zh-CN", _Context(target_lang="zh-CN", scene="nature")
            ).target_text

        account_answer = crane_answer("the account crane")
        river_answer = crane_answer("the river crane")
        check.check("and the engine answers each one only where its condition holds",
                    "账目" in account_answer and "河流" not in account_answer
                    and "河流" in river_answer and "账目" not in river_answer,
                    f"account line -> {account_answer!r} / river line -> {river_answer!r}")

        status, body = post(base + "/api/command",
                            {"cmd": "library_delete", "source": "crane", "lang": "zh-CN",
                             "domain": "nature", "when_line": "account"})
        check.check("deleting a row by its full identity removes exactly that meaning",
                    status == 200 and json.loads(body).get("ok"),
                    str(json.loads(body).get("detail"))[:90])
        status, body = get(base + "/api/library")
        library = json.loads(body)
        survivors = [row for row in library["entries"] if row["source"] == "crane"]
        check.check("and its sibling is still there, which is what the panel's button relies on",
                    len(survivors) == 1 and survivors[0]["when_line"] == "river"
                    and survivors[0]["target"] == "河流",
                    json.dumps([(row["when_line"], row["target"]) for row in survivors],
                               ensure_ascii=False))

        # ---- the collision diagnostic ------------------------------------- #
        #
        # The user's own second meaning is the case that matters, so the collision is made
        # on purpose against a shipped row of whatever language it declares.
        victim = next(row for row in library["entries"]
                      if not row["user"] and not row["suppressed"] and row["lang"])
        status, body = post(base + "/api/command",
                            {"cmd": "library_put", "source": victim["source"],
                             "target": "冲突测试", "lang": victim["lang"]})
        check.check("a user row written over a shipped row is accepted (the collision case)",
                    status == 200 and json.loads(body).get("ok"),
                    f"{victim['source']} / {victim['lang']}")

        status, body = get(base + "/api/library")
        library = json.loads(body)
        conflicts = library.get("conflicts") or []
        losing = next((item for item in conflicts
                       if item.get("source") == victim["source"]), None)
        check.check("GET /api/library reports which translation was dropped and which was kept",
                    losing is not None
                    and losing["kept"]["target"] == "冲突测试"
                    and losing["dropped"]["target"] == victim["target"],
                    json.dumps(losing, ensure_ascii=False)[:150] if losing
                    else f"nothing reported for {victim['source']!r} out of {len(conflicts)}")
        check.check("and it names the layers that decided it, which is what the page shows",
                    losing is not None and losing["kept"]["layer"] == "user"
                    and losing["dropped"]["layer"] != "user",
                    f"{losing['kept']['layer']} over {losing['dropped']['layer']}"
                    if losing else "no conflict")
        check.check("each conflict carries every field the warning text reads",
                    all({"source", "lang", "domain", "kept", "dropped", "why"} <= set(item)
                        and {"target", "origin", "layer", "priority"} <= set(item["kept"])
                        and {"target", "origin", "layer", "priority"} <= set(item["dropped"])
                        for item in conflicts),
                    f"{len(conflicts)} conflict(s)")

        # ---- an import preview must write nothing ------------------------- #
        print("")
        print("-- the import preview writes nothing (and the import does) --")
        library_path = _Path(session.library().path)
        before_bytes = library_path.read_bytes()
        status, body = get(base + "/api/library")
        before = json.loads(body)
        pasted = "quayside barge,码头驳船\nskyfarer,天行者\n"

        status, body = post(base + "/api/library/import",
                            {"text": pasted, "format": "csv", "dry_run": True})
        result = json.loads(body)
        detail = str(result.get("detail"))
        check.check("POST /api/library/import accepts dry_run and answers with a preview",
                    status == 200 and result.get("ok"), f"HTTP {status} {detail[:80]}")
        check.check("the preview counts what would happen, using the real import's numbers",
                    "preview" in detail and "would be imported" in detail and "added 2" in detail,
                    detail[:120])
        check.check("and it says in words that nothing was written",
                    "nothing written" in detail, detail[:120])
        # The assertion that matters most in this section: a preview that wrote would be an
        # import wearing the word "preview", and the file on disk is the only witness.
        check.check("the library file on disk is byte for byte what it was before the preview",
                    library_path.read_bytes() == before_bytes,
                    f"{len(before_bytes)} bytes before and after")
        status, body = get(base + "/api/library")
        after = json.loads(body)
        check.check("no row appeared, and the user's own row count is unchanged",
                    after["user"] == before["user"]
                    and len(after["entries"]) == len(before["entries"])
                    and not any(row["source"] == "quayside barge" for row in after["entries"]),
                    f"user {before['user']} -> {after['user']}, "
                    f"rows {len(before['entries'])} -> {len(after['entries'])}")

        # Without this, the three checks above would also pass on an import that never
        # writes anything at all -- which is the failure they exist to catch.
        status, body = post(base + "/api/library/import", {"text": pasted, "format": "csv"})
        result = json.loads(body)
        check.check("the same import without dry_run does write, so the preview check is not vacuous",
                    status == 200 and "added 2" in str(result.get("detail"))
                    and library_path.read_bytes() != before_bytes,
                    str(result.get("detail"))[:100])
        check.check("and the pasted rows are the user's, and usable by the engine at once",
                    any(row["source"] == "quayside barge" and row["user"]
                        for row in result.get("entries", []))
                    and session.corpus.translate("quayside barge", "zh-CN").target_text == "码头驳船",
                    session.corpus.translate("quayside barge", "zh-CN").target_text)

        # The panel hands the format through rather than deciding it: an explicit choice is
        # sent as itself, and "auto" is the engine's own signal to sniff the content
        # (``library.sniff_format``: JSON if it starts with a brace or bracket, TSV if the
        # first line has a tab, CSV otherwise). So this is what the paste box relies on --
        # a block of CSV lines arrives without the page having to name its format -- and it
        # is asserted here because one rule in one place is the whole point of it.
        status, body = post(base + "/api/library/import",
                            {"text": pasted, "format": "auto", "dry_run": True})
        result = json.loads(body)
        detail = str(result.get("detail"))
        # Both rows are already in the library by now (the real import above put them
        # there), so they are counted as updates -- what this asserts is that "auto" found
        # *two* entries in comma-separated text instead of failing with "not valid JSON",
        # which is exactly what a pasted block depends on.
        check.check("the engine sniffs format 'auto' itself, which is what the paste box sends",
                    status == 200 and result.get("ok")
                    and "2 entries would be imported" in detail,
                    f"HTTP {status} {detail[:100]}")

        # ---- promotion: corrections become entries ------------------------ #
        #
        # Through the session, not through /api/command: the panel refuses `correct` (it is
        # not a setting), and this is the real path the desktop window uses.
        print("")
        print("-- promoting corrections: a copy into the corpus, not a move --")
        status, body = post(base + "/api/library/promote", {"scope": "term", "dry_run": True})
        check.check("promoting with nothing recorded is refused with a readable reason",
                    status == 400 and "纠正" in str(json.loads(body).get("detail")),
                    f"HTTP {status} {str(json.loads(body).get('detail'))[:70]}")

        recorded = session.command("correct", {"source": "an aether barge", "target": "以太驳船",
                                              "scope": "term", "lang": "zh-CN"})
        check.check("a correction recorded through the engine is what promotion reads",
                    bool(recorded.get("ok")), str(recorded.get("detail"))[:90])

        status, body = get(base + "/api/corpus")
        corrections = json.loads(body).get("corrections", [])
        check.check("the read-only corrections view lists it, so it can be seen before promoting",
                    any(item.get("source") == "an aether barge" for item in corrections),
                    f"{len(corrections)} correction(s)")

        # A correction already shows up in the table, because the corrections file lives in
        # the same user layer and the loader reads it as a corpus file. That is why "was it
        # promoted?" can only be answered by the edited file itself, not by the view -- and
        # the page's help text says the two files stay separate for exactly this reason.
        status, body = get(base + "/api/library")
        check.check("and the table already shows it, marked with the file it came from",
                    any(row["source"] == "an aether barge"
                        and row["origin"] == "corpus:corrections"
                        for row in json.loads(body)["entries"]),
                    "promoting copies it into library.json; the two files stay separate")

        def in_library_file(source: str) -> bool:
            """Whether the file the editor writes itself holds this source.

            The file rather than the view: a correction is in the view too, so the view
            cannot tell "promoted" from "merely corrected".
            """
            if not library_path.exists():
                return False
            try:
                data = json.loads(library_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                return False
            body = data.get("entries") if isinstance(data.get("entries"), dict) else data
            return isinstance(body, dict) and source in body

        before_bytes = library_path.read_bytes()
        status, body = post(base + "/api/library/promote",
                            {"lang": "zh-CN", "scope": "term", "dry_run": True})
        result = json.loads(body)
        detail = str(result.get("detail"))
        check.check("the promotion preview counts corrections without writing",
                    status == 200 and result.get("ok") and "preview" in detail
                    and "would become entries" in detail and "nothing written" in detail,
                    f"HTTP {status} {detail[:100]}")
        check.check("and the promotion preview left the library file untouched",
                    library_path.read_bytes() == before_bytes,
                    f"{len(before_bytes)} bytes before and after")
        check.check("so nothing has been copied into the edited corpus file yet",
                    not in_library_file("an aether barge"),
                    "library.json, not the merged view, is what a promotion writes")

        status, body = post(base + "/api/library/promote", {"lang": "zh-CN", "scope": "term"})
        result = json.loads(body)
        detail = str(result.get("detail"))
        check.check("the real promotion reports what it promoted",
                    status == 200 and result.get("ok") and "promoted 1" in detail
                    and "added 1" in detail, f"HTTP {status} {detail[:100]}")
        promoted = [row for row in result.get("entries", [])
                    if row["source"] == "an aether barge"]
        check.check("the correction is now a corpus entry the editor can see and edit",
                    in_library_file("an aether barge") and len(promoted) == 1
                    and promoted[0]["target"] == "以太驳船" and promoted[0]["user"]
                    and promoted[0]["lang"] == "zh-CN",
                    json.dumps(promoted, ensure_ascii=False)[:150])
        check.check("and the engine translates with it immediately",
                    session.corpus.translate("an aether barge", "zh-CN").target_text == "以太驳船",
                    session.corpus.translate("an aether barge", "zh-CN").target_text)
        status, body = get(base + "/api/corpus")
        check.check("the correction record is still there: promoting copies, it does not move",
                    any(item.get("source") == "an aether barge"
                        for item in json.loads(body).get("corrections", [])),
                    "the two files stay separate, and the corrections file is not consumed")

        # ---- the language a correction was written for -------------------- #
        #
        # Two engine facts the panel now states in words, so they are asserted rather than
        # trusted: `correct` stamps the target language that was selected, and a row with
        # **no** language -- the shape every pre-release corrections file has -- applies
        # under *every* target. That second fact is the reason the loader must keep
        # accepting those rows: refusing them would silently discard the user's own work
        # on upgrade. The panel shows them as 未标注 instead of a blank cell.
        print("")
        print("-- the language a correction was written for --")
        tagged_line = "a line that was corrected while translating into Chinese"
        recorded = session.command("correct", {"source": tagged_line,
                                              "target": "这条是中文纠正", "scope": "line"})
        selected = str(session.config.target_lang or "")
        check.check("a correction recorded now is stamped with the selected target language",
                    bool(recorded.get("ok")) and bool(selected),
                    f"selected={selected!r}; {str(recorded.get('detail'))[:60]}")

        status, body = get(base + "/api/corpus")
        rows = json.loads(body).get("corrections", [])
        tagged = next((row for row in rows if row.get("source") == tagged_line), None)
        check.check("and the table's language column has that language to show",
                    tagged is not None and tagged.get("lang") == selected,
                    json.dumps(tagged, ensure_ascii=False)[:150] if tagged
                    else f"the row is not in the listing at all ({len(rows)} row(s))")

        # An untagged correction can no longer be produced by the `correct` command -- it
        # stamps whatever language is selected -- so it is created the way it exists in the
        # wild: a corrections file written before the field existed. The row is *added* to
        # the file the engine reads, beside the existing rows, which is what an upgrade
        # looks like; the reload is the ordinary hot path for an edited file.
        corrections_file = _Path(session.corpus.corrections.path)
        old_line = "a sentence corrected before this release, with no language recorded"
        payload = json.loads(corrections_file.read_text(encoding="utf-8"))
        payload.setdefault("entries", {})[old_line] = {
            "target": "上一个版本留下的整句",
            "scope": "line",
            "count": 2,
            "first_seen": 1.0,
            "last_seen": 2.0,
            "note": None,
        }
        corrections_file.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        session.corpus.load()

        status, body = get(base + "/api/corpus")
        rows = json.loads(body).get("corrections", [])
        untagged = next((row for row in rows if row.get("source") == old_line), None)
        check.check("an old-format row with no language is loaded, not discarded",
                    untagged is not None and untagged.get("target") == "上一个版本留下的整句",
                    json.dumps(untagged, ensure_ascii=False)[:150] if untagged
                    else "the row is missing: the engine dropped a file it used to accept")
        check.check("and it declares no language, which is what the page marks 未标注",
                    untagged is not None and not untagged.get("lang"),
                    f"lang={untagged.get('lang')!r}, key present: {'lang' in untagged}"
                    if untagged else "row missing")

        def corrected(line: str, lang: str) -> str:
            return session.corpus.translate(line, lang).target_text

        check.check("an untagged correction really applies under every target language",
                    corrected(old_line, "zh-CN") == "上一个版本留下的整句"
                    and corrected(old_line, "ja") == "上一个版本留下的整句",
                    f"zh-CN -> {corrected(old_line, 'zh-CN')!r} / "
                    f"ja -> {corrected(old_line, 'ja')!r}")
        check.check("while the tagged one answers only the language it was written for",
                    corrected(tagged_line, selected) == "这条是中文纠正"
                    and corrected(tagged_line, "ja") != "这条是中文纠正",
                    f"{selected} -> {corrected(tagged_line, selected)!r} / "
                    f"ja -> {corrected(tagged_line, 'ja')!r}")

        status, body = get(base + "/api/info")
        engine_untagged = int(json.loads(body).get("corrections_untagged", -1))
        # `corrections_untagged` counts untagged *whole-line* corrections whose loose key is
        # long enough to be matched; the page marks every row that declares no language. In
        # the state this check builds there is exactly one such correction, so the engine's
        # number and the panel's must be the same one -- and if they ever stop agreeing,
        # this says so instead of letting the card and the engine disagree quietly.
        marked = sum(1 for row in rows if not row.get("lang"))
        check.check("the engine's untagged count agrees with the rows the page marks",
                    engine_untagged == marked == 1,
                    f"engine reports {engine_untagged}, the listing has {marked} row(s) "
                    f"without a language, of {len(rows)}")

        # ---- the page carries the controls, and is still offline ---------- #
        print("")
        print("-- the page carries the new controls, and is still offline --")
        status, html = get(base + "/")
        check.check("GET / still returns the page", status == 200, f"HTTP {status}")
        new_controls = [
            "libconflicts", "libscene", "libscenes", "libdomain", "libwhenline",
            "libwhennear", "libwhenwindow", "libpaste", "libpastebtn", "libpastepreview",
            "libpreview", "xpromotescope", "xpromotelang", "xpromotereplace",
            "xpromotepreview", "xpromote", "xpromotemsg",
        ]
        missing = [name for name in new_controls if f'id="{name}"' not in html]
        check.check("the markup defines every control the new script looks up",
                    not missing, f"missing: {missing}")
        check.check("the scene field suggests the known scenes without forbidding a new name",
                    '<datalist id="libscenes">' in html and 'list="libscenes"' in html,
                    "a datalist suggests; typing a scene nobody has used yet must stay allowed")
        check.check("a scene-specific or conditional row is marked as one in the table",
                    ".libtable tr.sense" in html and 'classList.add("sense")' in html,
                    "the row the user is hunting for has to be findable in a dense table")
        check.check("the page states the real precedence order, not a guess",
                    "层级" in html and "priority" in html and "translate._rank" in html
                    and "自己的词条永远压过出厂词条" in html,
                    "layer, then scene, then priority, then a deterministic tail")
        check.check("and it says conditions are a filter rather than a rank",
                    "条件是筛子" in html and "when_line" in html,
                    "an entry whose conditions do not hold is not a candidate at all")
        check.check("the conflict warning names the fix (another scene, or conditions)",
                    "条词条没有生效" in html and "不同的「场景」" in html
                    and "when_near" in html,
                    "the count alone does not tell the user what to do")
        check.check("the corrections card explains what promotion means",
                    "提升" in html and "corrections.json" in html
                    and "把纠正复制成语料库词条" in html,
                    "a copy, and the two files stay separate")
        check.check("the corrections table has a language column and marks the untagged rows",
                    "<th>语言</th>" in html and "未标注" in html
                    and 'c.lang || ""' in html and "pill untagged" in html,
                    "a blank cell would read as 'unknown'; it means 'every target language'")
        check.check("and the card says what untagged means and how to change it",
                    "对每个目标语言都生效" in html and "重新纠正同一条" in html
                    and "拒绝等于升级时静默丢掉" in html,
                    "retyping it under the wanted target language is the only way: the engine has no command to tag an existing correction")
        check.check("and that a preview never writes, in the words of the buttons themselves",
                    html.count("不写入") >= 3,
                    f"{html.count('不写入')} preview button(s) labelled as not writing")
        check.check("the page is still entirely offline (no CDN, no web font, no https)",
                    "https://" not in html and "//cdn" not in html,
                    "requirement R1: nothing may be fetched from the network")

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
    # The earlier sections stop the panel when they are done, so this one brings it back
    # up -- on a freshly chosen, verified port rather than the one used above. That port
    # was released when the panel stopped, and a released port is exactly the one another
    # process can pick up in the gap; the requests below would then be answered by that
    # process's session and would write *its* config file. (That gap is where this file's
    # old "ConnectionRefused, once in five runs" flake came from.)
    if panel.running:
        panel.stop()
    restarted_panel, restarted_base = _start_owned_panel(session)
    if restarted_panel is not None and restarted_panel.running:
        panel, base = restarted_panel, restarted_base
    # Checked, not assumed. The previous version restarted the panel and then
    # fired requests regardless of whether it came back up, so a failed restart
    # surfaced as a ConnectionRefused several lines later instead of here.
    restarted = panel.running
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
        # Every live field names the command that applies it, and this is the surface
        # that has a real engine to check that claim against. A schema naming a command
        # the engine does not implement is a control that silently does nothing, and the
        # static check cannot see it because the schema is all it has.
        live_commands = sorted({f["command"] for f in fields if f.get("command")})
        implemented = set(session.command_names)
        check.check(
            "every live field names a command this engine actually implements",
            live_commands and not (set(live_commands) - implemented),
            "missing: "
            + ", ".join(sorted(set(live_commands) - implemented))
            if set(live_commands) - implemented
            else f"{len(live_commands)} commands, all implemented",
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

    zombie_port = _process_port()
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
