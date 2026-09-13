#!/usr/bin/env python3
"""Self-capture verification. Headless: no screen, no models.

"Screen recognition must exclude itself" is two problems wearing one coat, and they need
different answers:

* **A window this program owns** -- the overlay, the control window. Windows can be asked
  to keep it out of capture entirely (``WDA_EXCLUDEFROMCAPTURE``), which is what the
  overlay has always done and what the control window now does too.
* **A window this program does not own** -- a browser showing the web panel. There is
  nothing to ask: the window belongs to another process. The only honest options are to
  notice it and refuse to read it, or to OCR the user's own settings page forever. The
  user's report was the second one: the program stuttering from the first frame because
  change detection never settles on a window that repaints its own counters.

So this checks the geometry (is our window inside the region), the filter (which windows
count), the hold (does the engine refuse to start), and the two settings that turn the
guards off. The window list is injected rather than enumerated, so the logic is verified
on any machine -- including one where nothing of ours is on screen.

    run.cmd selfcheck_selfcapture --summary
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from watashi.checks import Checker  # noqa: E402

from watashi.capture import Region
from watashi.events import CMD_SET_SELF_CAPTURE, CMD_SET_REGION, EVENT_ERROR
from watashi.config import AppConfig
from watashi.session import Session
from watashi.synth import SyntheticCapturer
from watashi.winutil import WindowInfo, own_ui_over

REGION = Region(0, 800, 1000, 200)


def window(
    title: str,
    rect: tuple[int, int, int, int],
    *,
    pid: int = 12345,
    minimized: bool = False,
    hwnd: int = 1,
) -> WindowInfo:
    return WindowInfo(
        hwnd=hwnd, title=title, class_name="Probe", pid=pid, process="probe.exe",
        rect=rect, minimized=minimized,
    )


def main() -> int:
    check = Checker()
    print("=" * 78)
    print("Self-capture self check (no screen, no models)")
    print("=" * 78)

    # ---------------------------------------------------------------- #
    check.section("rectangle geometry")

    check.check(
        "a region inside another is found",
        REGION.intersection(Region(100, 850, 200, 100)) is not None,
    )
    shared = REGION.intersection(Region(900, 900, 400, 100))
    check.check(
        "the overlap is the overlap, not either rectangle",
        shared == Region(900, 900, 100, 100),
        f"got {shared}",
    )
    check.check(
        "touching edges do not overlap",
        REGION.intersection(Region(1000, 800, 100, 200)) is None,
        "a shared edge has no area, and a window exactly beside the region is not in it",
    )
    check.check(
        "a rectangle far away does not overlap",
        REGION.intersection(Region(0, 0, 100, 100)) is None,
    )
    check.check(
        "the ratio is measured against the region, not the window",
        abs(REGION.overlap_ratio(Region(900, 900, 400, 100)) - 0.05) < 1e-9,
        f"{REGION.overlap_ratio(Region(900, 900, 400, 100))}",
    )
    check.check(
        "a window covering the region is 1.0",
        abs(REGION.overlap_ratio(Region(-100, -100, 5000, 5000)) - 1.0) < 1e-9,
    )
    check.check("no overlap is 0.0", REGION.overlap_ratio(Region(0, 0, 10, 10)) == 0.0)
    check.check(
        "an empty region cannot overlap anything",
        Region(0, 0, 0, 0).overlap_ratio(Region(0, 0, 100, 100)) == 0.0,
        "division by zero would be the other outcome",
    )

    # ---------------------------------------------------------------- #
    check.section("which windows count as our own")

    ours = window("Project Watashi", (0, 800, 1000, 200), pid=999)
    browser = window("Project Watashi · 设置与阅览 — Chromium", (0, 700, 1200, 400), pid=500)
    other = window("Notepad", (0, 800, 1000, 200), pid=500)
    elsewhere = window("Project Watashi", (0, 0, 400, 200), pid=999)
    tiny = window("Project Watashi", (0, 800, 20, 20), pid=999)

    found = own_ui_over(REGION, marker="Project Watashi", own_pid=999,
                        windows=[ours, browser, other, elsewhere, tiny])
    titles = [w.title for w, _ratio in found]
    check.check(
        "the browser showing our panel is found, by title",
        any(w.title.startswith("Project Watashi ·") for w, _ in found),
        f"{titles}",
    )
    check.check(
        "and it is found even though its process is not ours",
        all(w.pid != 999 for w, _ in found if w.title.startswith("Project Watashi ·")),
        "the case exclusion cannot solve",
    )
    check.check(
        "our own window is found, by process",
        any(w.pid == 999 and "Notepad" not in w.title for w, _ in found),
    )
    check.check(
        "an unrelated window is not",
        not any(w.title == "Notepad" for w, _ in found),
        str(titles),
    )
    check.check(
        "a window of ours outside the region is not",
        not any(w.rect == elsewhere.rect for w, _ in found),
        "the check is about the region, not about the window existing",
    )
    check.check(
        "results are ordered by how much they cover",
        [ratio for _w, ratio in found] == sorted((r for _w, r in found), reverse=True),
        str([round(r, 2) for _w, r in found]),
    )
    check.check(
        "a barely-touching window is below the threshold",
        not any(w.rect == Region(999, 800, 100, 200).as_tuple() for w, _ in found),
    )

    # The distinction that makes the two halves work together: a window that already
    # excludes itself from capture cannot appear in the frame, so holding for it would be
    # a false alarm.
    check.check(
        "a minimized window is ignored",
        not own_ui_over(REGION, marker="Project Watashi", own_pid=999,
                        windows=[window("Project Watashi", (0, 800, 1000, 200),
                                        pid=999, minimized=True)]),
    )
    check.check(
        "the marker is matched case-insensitively",
        bool(own_ui_over(REGION, marker="project watashi", own_pid=None,
                         windows=[window("PROJECT WATASHI", (0, 800, 1000, 200))])),
        "a window title is not something to be strict about",
    )
    check.check(
        "an empty marker matches nothing by title",
        not own_ui_over(REGION, marker="", own_pid=None,
                        windows=[window("anything", (0, 800, 1000, 200))]),
        "an empty string is in every title, which would hold for every window",
    )
    check.check(
        "with no pid to match and no marker, nothing is ours",
        not own_ui_over(REGION, marker="", own_pid=None, windows=[other]),
    )

    # ---------------------------------------------------------------- #
    check.section("the engine holds instead of reading its own window")

    config = AppConfig.load()
    config.capture["hold_if_self_visible"] = True
    config.translation["nmt_model"] = None
    session = Session(config, capturer=SyntheticCapturer(hold_seconds=0.5))
    session.build()
    channel = session.subscribe()

    def events() -> list[dict]:
        found_events = []
        while True:
            try:
                found_events.append(channel.get_nowait())
            except Exception:
                return found_events

    real_findings = Session.find_self_over_region(session)
    check.check(
        "the real detector runs against the real desktop without raising",
        isinstance(real_findings, list),
        f"{len(real_findings)} window(s) of ours over the region right now"
        f"{': ' + real_findings[0][0].title if real_findings else ''}",
    )

    # Injected rather than enumerated: the logic under test is "given these windows and
    # this region, hold or not".
    session.__dict__["_detect_self_windows"] = lambda: [
        (window("Project Watashi · 设置与阅览", (0, 0, 100, 100)), 0.6)
    ]
    session._pipeline.pause()
    check.check("the pipeline starts paused for the fixture", session.pipeline.paused)
    session._pipeline.resume()
    events()
    held = session.hold_if_capturing_self()
    check.check("it reports that it held", held is True)
    check.check("and the pipeline really is paused", session.pipeline.paused is True)
    check.check(
        "the reason names the window and how much it covers",
        "Project Watashi" in session._status and "60%" in session._status,
        session._status[:120],
    )
    kinds = [event.get("type") for event in events()]
    check.check(
        "an error event carries it to every surface, not just the status line",
        EVENT_ERROR in kinds,
        str(kinds),
    )

    session.__dict__["_detect_self_windows"] = lambda: []
    session._pipeline.resume()
    check.check(
        "with nothing of ours over the region it does not hold",
        session.hold_if_capturing_self() is False,
        "a guard that always pauses is a guard nobody keeps",
    )
    check.check("and the pipeline is left running", session.pipeline.paused is False)

    # ---------------------------------------------------------------- #
    check.section("the settings that turn the guards off")

    # The detection is stubbed, not the policy: `find_self_over_region` still decides
    # whether to look at all, which is the setting under test here.
    session.__dict__["_detect_self_windows"] = lambda: [
        (window("Project Watashi", (0, 0, 100, 100)), 0.5)
    ]
    result = session.command(CMD_SET_SELF_CAPTURE, {"hold_if_self_visible": False})
    check.check("hold_if_self_visible can be switched off", result.get("ok"), str(result.get("detail"))[:60])
    check.check(
        "and then the engine does not hold even with our window over the region",
        session.hold_if_capturing_self() is False and session.pipeline.paused is False,
        "someone who wants to translate our own UI text has to be able to",
    )
    result = session.command(CMD_SET_SELF_CAPTURE, {"hold_if_self_visible": True})
    check.check("switching it back on checks immediately", result.get("ok"), str(result.get("detail"))[:80])
    check.check(
        "and holds, because the condition is true right now",
        session.pipeline.paused is True and "paused" in str(result.get("detail")),
        str(result.get("detail"))[:80],
    )
    result = session.command(CMD_SET_SELF_CAPTURE, {"exclude_self": False})
    check.check("exclude_self can be switched off too", result.get("ok"), str(result.get("detail"))[:80])
    check.check(
        "and the reply admits it only affects windows created later",
        "from now on" in str(result.get("detail")),
        str(result.get("detail"))[:100],
    )
    check.check(
        "an empty set_self_capture is refused",
        not session.command(CMD_SET_SELF_CAPTURE, {}).get("ok"),
    )

    # ---------------------------------------------------------------- #
    check.section("resuming is not blocked, only informed")

    session._pipeline.resume()
    session._status = ""
    session.__dict__["_detect_self_windows"] = lambda: [
        (window("Project Watashi · 设置与阅览", (0, 0, 100, 100)), 0.7)
    ]
    detail = session._handlers()[CMD_SET_REGION]({"region": "10,20,300,180"})
    check.check(
        "changing the region re-asks the question, and holds while it is still true",
        "paused" in detail and session.pipeline.paused is True,
        detail[:80],
    )
    session.__dict__["_detect_self_windows"] = lambda: []
    session._pipeline.resume()
    detail = session._handlers()[CMD_SET_REGION]({"region": "10,20,300,180"})
    check.check(
        "and a region that is clear says so instead of pausing",
        "paused" not in detail,
        detail[:80],
    )
    check.check(
        "leaving the pipeline as the user left it: a region change never resumes it",
        session.pipeline.paused is False,
        "it was resumed above deliberately, and the command did not change that",
    )

    session.__dict__["_detect_self_windows"] = lambda: [
        (window("Project Watashi", (0, 0, 100, 100)), 0.7)
    ]
    session._pipeline.pause()
    detail = session._handlers()["resume"]({})
    check.check(
        "resume runs even when our window is over the region",
        session.pipeline.paused is False,
        "the user has decided; blocking them again is the program arguing with its user",
    )
    check.check(
        "but it says what it sees",
        "提醒" in session._status and "Project Watashi" in session._status,
        session._status[:110],
    )
    check.check("and the command result says so too", "own window" in detail, detail)

    session.stop()
    return check.report()


if __name__ == "__main__":
    sys.exit(main())
