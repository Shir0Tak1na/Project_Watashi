"""Project Watashi -- local real time screen translation prototype.

This package is the M1 validation prototype described in
``docs/requirements`` and ``Project Watashi.md``. It exists to prove three
things end to end on a real machine:

1. screen region capture -> OCR -> translation -> on screen overlay works and
   stays fully local (R1);
2. corpora and rules are plain files that can be edited while the program runs
   (R2, R3);
3. the latency budget in R4 is reachable in practice, and is measured rather
   than assumed.

It is deliberately Python and deliberately disposable: the goal is to validate
the pipeline and the latency numbers before committing the same design to the
C++ core.
"""

#: Pre-release. This is the Python validation prototype, not the C++ product the
#: requirements describe, and the version says so rather than implying otherwise:
#: 0.0.x means "usable for testing", not "feature complete".
#:
#: It previously read 0.1.0, which was never tagged or released. Re-numbering into
#: 0.0.x is deliberate: 0.1.0 would claim more maturity than a prototype that has
#: not been ported deserves.
__version__ = "0.0.4"

#: Shown next to the version so no one has to guess what they are running.
RELEASE_STAGE = "测试版 (pre-release prototype)"

__all__ = [
    "capture",
    "ocr",
    "overlay",
    "pipeline",
    "translate",
]
