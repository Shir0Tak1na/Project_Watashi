#!/usr/bin/env python3
"""Isolate where OCR latency actually goes.

Times raw ONNX Runtime separately from the RapidOCR wrapper so a slow machine
can be told apart from a slow pipeline stage. Run this before trusting any
latency number reported by the prototype.
"""

from __future__ import annotations

import statistics
import sys
import time
from pathlib import Path

import numpy as np

PKG = Path(__file__).resolve().parent.parent / ".venv" / "Lib" / "site-packages"
if not PKG.exists():
    PKG = Path(sys.prefix) / "Lib" / "site-packages"


def bench(fn, repeats: int = 5) -> tuple[float, float]:
    fn()  # warm
    samples = []
    for _ in range(repeats):
        started = time.perf_counter()
        fn()
        samples.append((time.perf_counter() - started) * 1000.0)
    return statistics.median(samples), max(samples)


def main() -> int:
    import onnxruntime as ort

    models = PKG / "rapidocr_onnxruntime" / "models"
    det = models / "ch_PP-OCRv4_det_infer.onnx"
    rec = models / "ch_PP-OCRv4_rec_infer.onnx"

    print("=" * 72)
    print("ONNX Runtime diagnosis")
    print("=" * 72)
    print(f"  ort version      : {ort.__version__}")
    print(f"  providers        : {ort.get_available_providers()}")
    print(f"  models dir       : {models}")
    print(f"  det model exists : {det.exists()}")
    print(f"  rec model exists : {rec.exists()}")
    print("")

    options = ort.SessionOptions()
    print(f"  default intra_op : {options.intra_op_num_threads} (0 = ORT default)")
    print(f"  default inter_op : {options.inter_op_num_threads}")

    # --- session creation cost ------------------------------------------ #
    started = time.perf_counter()
    session = ort.InferenceSession(str(det), providers=["CPUExecutionProvider"])
    create_ms = (time.perf_counter() - started) * 1000.0
    print(f"  det session load : {create_ms:.0f} ms")

    inputs = session.get_inputs()
    print("")
    print("  det inputs:")
    for item in inputs:
        print(f"    {item.name:12s} {item.shape} {item.type}")
    outputs = session.get_outputs()
    print("  det outputs:")
    for item in outputs:
        print(f"    {item.name:12s} {item.shape} {item.type}")

    name = inputs[0].name
    input_shape = [d if isinstance(d, int) and d > 0 else 640 for d in inputs[0].shape]
    input_shape[0] = 1
    if len(input_shape) != 4:
        input_shape = [1, 3, 640, 640]
    print("")
    print(f"  synthesised input: {input_shape}")

    tensor = np.random.rand(*input_shape).astype(np.float32)

    def run_det() -> None:
        session.run(None, {name: tensor})

    median, worst = bench(run_det, repeats=3)
    print(f"  RAW det inference: median {median:.1f} ms  max {worst:.1f} ms")
    if median > 0:
        print(f"  RAW det implied  : {1000.0 / median:.2f} FPS")

    # --- ONNX Runtime thread settings ----------------------------------- #
    print("")
    print("-- thread sweep on the raw det model --")
    for threads in (1, 2, 4, 8, 16):
        opts = ort.SessionOptions()
        opts.intra_op_num_threads = threads
        opts.inter_op_num_threads = 1
        opts.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        sess = ort.InferenceSession(str(det), sess_options=opts, providers=["CPUExecutionProvider"])
        sess.run(None, {name: tensor})
        median, _ = bench(lambda s=sess: s.run(None, {name: tensor}), repeats=3)
        print(f"  intra_op={threads:>2}  ->  {median:8.1f} ms   ({1000.0 / median:.2f} FPS)")

    # --- rec model ------------------------------------------------------- #
    print("")
    started = time.perf_counter()
    rec_session = ort.InferenceSession(str(rec), providers=["CPUExecutionProvider"])
    print(f"  rec session load : {(time.perf_counter() - started) * 1000.0:.0f} ms")
    rec_inputs = rec_session.get_inputs()
    for item in rec_inputs:
        print(f"    rec input {item.name:12s} {item.shape} {item.type}")
    rec_shape = [d if isinstance(d, int) and d > 0 else 320 for d in rec_inputs[0].shape]
    rec_shape[0] = 1
    if len(rec_shape) != 4:
        rec_shape = [1, 3, 48, 320]
    rec_tensor = np.random.rand(*rec_shape).astype(np.float32)

    def run_rec() -> None:
        rec_session.run(None, {rec_inputs[0].name: rec_tensor})

    median, worst = bench(run_rec, repeats=3)
    print(f"  RAW rec inference: median {median:.1f} ms  max {worst:.1f} ms ({rec_shape})")

    print("")
    print("-- verdict --")
    if median + 0 < 100:
        print("  ONNX Runtime is healthy on this machine; the bottleneck is in")
        print("  preprocessing or in how many crops the detector produced.")
    else:
        print("  ONNX Runtime itself is slow here. Check for CPU throttling,")
        print("  a power plan that parks cores, or a machine without AVX2.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
