#!/usr/bin/env python3
"""Stability gate verification. Headless: no screen, no models, no sleeping.

The gate decides *when* OCR runs, and the difference it makes is invisible in the
output -- a wrong gate still produces plausible-looking subtitles, just ones
recognised from mid-animation frames. So it is asserted numerically here instead.

Three properties matter, and the third is the one that makes the other two mean
anything:

1. **settle_s = 0 reproduces the old behaviour exactly.** Turning the gate on must
   be opt-in; if 0 did not behave like "fire on the changing frame", every existing
   latency number would silently change meaning.
2. **With a gate, a moving frame produces no OCR at all**, and a frame that then
   holds still produces exactly one -- and only after the settle delay. "Exactly
   one" matters: a gate that fires every frame after settling would be worse than
   no gate, because it would burn OCR on identical frames forever.
3. **The same scenario with the gate off produces many OCR runs.** Without this
   control, a passing "0 runs while moving" assertion could just mean the scenario
   never changed anything.

The clock is injected, so this runs in milliseconds and never flakes on a loaded
machine -- a gate tested with real sleeps is a test that fails on a busy CI box.

    run.cmd selfcheck_settle
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import numpy as np

from watashi.checks import Checker  # noqa: E402

from watashi.capture import ChangeDetector


def frame(value: int, width: int = 120, height: int = 40) -> np.ndarray:
    """A flat frame. Flat is deliberate: the diff between two of them is exactly
    the difference in `value`, so the threshold arithmetic is predictable."""
    return np.full((height, width, 3), value, dtype=np.uint8)


#: Frame period used by every scenario, i.e. a 10 fps capture loop.
STEP = 0.1


def run_scenario(
    detector: ChangeDetector,
    values: list[int],
    start: float = 100.0,
) -> list[tuple[float, bool, float]]:
    """Feed one frame per STEP. Returns (t, should_ocr, diff) per frame."""
    out = []
    now = start
    for value in values:
        fired, diff = detector.update(frame(value), now=now)
        out.append((now, fired, diff))
        now += STEP
    return out


def main() -> int:
    check = Checker()
    print("=" * 78)
    print("Stability gate self check (no screen, no models, no sleeping)")
    print("=" * 78)

    # ---------------------------------------------------------------- #
    check.section("settle_s = 0 keeps the original behaviour")
    plain = ChangeDetector(threshold=2.0, settle_s=0.0)
    # value jumps once, then holds
    trace = run_scenario(plain, [10] + [200] * 6)
    fires = [index for index, (_, fired, _) in enumerate(trace) if fired]
    check.check(
        "the first frame fires (nothing to compare against yet)",
        fires[:1] == [0],
        f"fires at {fires}",
    )
    check.check(
        "the changing frame fires",
        1 in fires,
        f"fires at {fires}",
    )
    check.check(
        "the held frames do not fire again",
        fires == [0, 1],
        f"fires at {fires}",
    )
    check.check(
        "no frame was reported as a settle wait",
        plain.settle_waits == 0,
        f"settle_waits={plain.settle_waits}",
    )

    # ---------------------------------------------------------------- #
    check.section("with a gate, motion produces no OCR")
    gated = ChangeDetector(threshold=2.0, settle_s=0.3)
    # 10 frames of hard motion (a scroll), then 8 frames held still
    moving = [10, 90, 170, 250, 60, 140, 220, 40, 120, 200]
    held = [55] * 8
    trace = run_scenario(gated, moving + held)
    during_motion = [fired for index, (_, fired, _) in enumerate(trace) if index < len(moving)]
    check.check(
        "nothing is recognised while the frame is moving",
        not any(during_motion),
        f"{sum(during_motion)} fire(s) during motion",
    )
    check.check(
        "held-back frames are counted, so the gate is visibly working",
        gated.settle_waits >= len(moving),
        f"settle_waits={gated.settle_waits}",
    )

    after = [(index, fired) for index, (_, fired, _) in enumerate(trace) if index >= len(moving)]
    fired_after = [index for index, fired in after if fired]
    check.check(
        "settling releases exactly one OCR run",
        len(fired_after) == 1,
        f"fires at {fired_after}",
    )
    if fired_after:
        # Take the reference from the data rather than assuming which frame changed
        # last: the transition from the moving sequence to the held one is itself a
        # change (the final moving value is not the held value), so the last change
        # is one frame later than "the end of the moving list". An earlier version
        # of this check hardcoded the wrong index and reported a spurious failure.
        last_change = max(
            index for index, (_, _, diff) in enumerate(trace) if diff >= gated.threshold
        )
        elapsed = (fired_after[0] - last_change) * STEP
        check.check(
            "and not before the settle delay has passed",
            elapsed >= 0.3,
            f"fired {elapsed:.2f}s after the last change (settle 0.30s)",
        )
        check.check(
            "nor more than a frame later, so it is not simply idling",
            elapsed < 0.3 + 2 * STEP,
            f"fired {elapsed:.2f}s after the last change of frame #{last_change}",
        )

    # ---------------------------------------------------------------- #
    check.section("a new change re-arms the gate")
    # Continue the same detector: hold, then move again, then hold.
    before = gated.accepted
    second = run_scenario(gated, [250, 40, 200, 60] + [70] * 8, start=200.0)
    second_fires = [index for index, (_, fired, _) in enumerate(second) if fired]
    check.check(
        "the second settling also fires exactly once",
        len(second_fires) == 1,
        f"fires at {second_fires}",
    )
    check.check(
        "and the accepted count went up by one",
        gated.accepted == before + 1,
        f"accepted {before} -> {gated.accepted}",
    )

    # ---------------------------------------------------------------- #
    check.section("CONTROL: the same scenario with the gate off fires repeatedly")
    control = ChangeDetector(threshold=2.0, settle_s=0.0)
    control_trace = run_scenario(control, moving + held)
    control_fires = sum(1 for _, fired, _ in control_trace if fired)
    check.check(
        "the control scenario really does change (so the 0 above is meaningful)",
        control_fires >= len(moving),
        f"{control_fires} fire(s) without a gate vs 1 with",
    )
    check.check(
        "the gate is what suppressed them, not an inert scenario",
        control.accepted > gated.accepted,
        f"control accepted={control.accepted}, gated accepted={gated.accepted}",
    )

    # ---------------------------------------------------------------- #
    check.section("reset() restarts the settle timer rather than releasing OCR")
    # The control is what makes this meaningful: by now the detector has been idle
    # long enough to have a large stability age, so a reset that failed to clear
    # the timer would release the very next frame immediately.
    stale_age = gated.stability_s
    check.check(
        "before the reset the detector reports a large stability age",
        stale_age >= 0.3,
        f"stability_s={stale_age:.2f}",
    )
    gated.reset()
    check.check("no pending change survives a reset", not gated.armed())
    check.check("stability age is cleared", gated.stability_s == 0.0)
    check.check(
        "and no old change timestamp survives, which is what would release OCR early",
        gated._last_change_at is None,  # noqa: SLF001 - the invariant under test
    )

    fired, _ = gated.update(frame(70), now=1000.0)
    check.check(
        "the first frame after a reset is held, not released on stale state",
        not fired,
        "a frame with nothing to compare against counts as a change, so the gate waits",
    )
    # ...and it is released once the delay has actually elapsed.
    released = False
    now = 1000.0
    for _ in range(6):
        now += STEP
        released, _ = gated.update(frame(70), now=now)
        if released:
            break
    check.check(
        "and released once the settle delay has really passed",
        released,
        f"fired after {now - 1000.0:.2f}s of holding still",
    )

    # With the gate off, the same frame is released immediately -- the difference
    # is the gate, not the frame.
    ungated = ChangeDetector(threshold=2.0, settle_s=0.0)
    ungated.reset()
    fired_plain, _ = ungated.update(frame(70), now=1000.0)
    check.check(
        "CONTROL: with settle_s = 0 the same frame fires immediately",
        fired_plain,
    )

    check.section("metrics")
    check.check(
        "settle_waits, accepted and stability_s are all exposed",
        isinstance(gated.settle_waits, int)
        and isinstance(gated.accepted, int)
        and isinstance(gated.stability_s, float),
    )

    return check.report()


if __name__ == "__main__":
    raise SystemExit(main())
