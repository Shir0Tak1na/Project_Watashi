#!/usr/bin/env python3
"""Download a local model for Project Watashi (no cloud inference).

This fetches weights once, so that the running application never needs the
network (requirement R1). After this completes, translation and speech
recognition are 100% local.

Translation weights go into ``models/llm/``; ``--asr`` fetches speech weights
into ``models/whisper/`` instead.

Default model: NLLB-200-distilled-600M, already quantized to int8 and
converted to CTranslate2 format (~617 MiB). It is a purpose-built neural
machine translation model rather than a chat LLM, which matters here:

* it is dramatically faster on CPU than a small chat model (R4),
* it is a real translator, so sentences come out grammatical instead of the
  word-by-word passthrough the corpus-only path produces,
* it covers 200 languages including eng_Latn and zho_Hans.

Usage
-----
    run.cmd fetch_model                 # download into ../models/llm/<name>
    run.cmd fetch_model --check         # report what is already present
    run.cmd fetch_model --force         # re-download everything
    run.cmd fetch_model --repo <id> --name <dir>

    run.cmd fetch_model --asr small      # speech weights for live audio
    run.cmd fetch_model --asr small --check
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
LOCK_FILE = REPO_ROOT / "models.lock.json"

DEFAULT_REPO = "JustFrederik/nllb-200-distilled-600M-ct2-int8"
DEFAULT_NAME = "nllb-200-distilled-600M-ct2-int8"

#: Every file CTranslate2 plus the tokenizer needs. LICENSE/README are skipped.
MODEL_FILES = (
    "config.json",
    "model.bin",
    "shared_vocabulary.txt",
    "sentencepiece.bpe.model",
    "special_tokens_map.json",
    "tokenizer.json",
    "tokenizer_config.json",
)


def human(size: float) -> str:
    for unit in ("B", "KiB", "MiB", "GiB"):
        if abs(size) < 1024 or unit == "GiB":
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GiB"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def lock_root(model: dict) -> Path | None:
    """Where a locked model lives: repo-relative, or inside an installed package."""
    package = model.get("package")
    if package:
        spec = importlib.util.find_spec(str(package))
        if spec is None or not spec.submodule_search_locations:
            return None
        return Path(next(iter(spec.submodule_search_locations)))
    path = model.get("path")
    return REPO_ROOT / str(path) if path else None


def verify_models_lock() -> int:
    """Check every file in models.lock.json. Returns 0 only when all match.

    Size and SHA-256 are both checked: a byte flip that leaves the size unchanged
    is exactly the case size alone would miss.
    """
    if not LOCK_FILE.exists():
        print(f"models.lock.json: MISSING ({LOCK_FILE})")
        return 1
    import json

    try:
        lock = json.loads(LOCK_FILE.read_text(encoding="utf-8"))
    except Exception as exc:
        print(f"models.lock.json: unreadable: {exc}")
        return 1
    models = lock.get("models") or []
    print(f"models.lock.json: {len(models)} model(s), lock_version={lock.get('lock_version')}")
    failures = 0
    for model in models:
        root = lock_root(model)
        model_id = model.get("id", "?")
        if root is None:
            print(f"  MISSING  {model_id}: package/path not found")
            failures += 1
            continue
        for item in model.get("files") or []:
            relative = item.get("path", "?")
            path = root / relative
            label = f"{model_id}/{relative}"
            if not path.exists():
                print(f"  MISSING  {label}")
                failures += 1
                continue
            size = path.stat().st_size
            if size != item.get("size"):
                print(
                    f"  SIZE     {label}: {size} != {item.get('size')} (locked)"
                )
                failures += 1
                continue
            digest = sha256_file(path)
            if digest != item.get("sha256"):
                print(
                    f"  SHA256   {label}: {digest[:16]}... != "
                    f"{str(item.get('sha256'))[:16]}... (locked)"
                )
                failures += 1
            else:
                print(f"  OK       {label}  {human(size)}  {digest[:16]}...")
    if failures:
        print(f"  {failures} file(s) do not match models.lock.json")
        return 1
    print("  every file matches models.lock.json")
    return 0


def remote_size(session, url: str) -> int | None:
    """Ask the server how big a file is, via a ranged HEAD/GET."""
    try:
        response = session.head(url, allow_redirects=True, timeout=30)
        if response.status_code == 200:
            length = response.headers.get("Content-Length")
            return int(length) if length else None
    except Exception:
        pass
    try:
        response = session.get(url, headers={"Range": "bytes=0-0"}, timeout=30)
        content_range = response.headers.get("Content-Range")
        if content_range and "/" in content_range:
            return int(content_range.rsplit("/", 1)[1])
    except Exception:
        pass
    return None


def download(session, url: str, dest: Path, expected: int | None, force: bool) -> bool:
    """Download with resume support. Returns True when the file is complete."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_suffix(dest.suffix + ".part")

    if dest.exists() and not force:
        size = dest.stat().st_size
        if expected is None or size == expected:
            print(f"    present  {dest.name:28s} {human(size)}")
            return True
        print(f"    size mismatch, re-fetching {dest.name} ({human(size)} != {human(expected)})")

    have = part.stat().st_size if part.exists() else 0
    if force and part.exists():
        part.unlink()
        have = 0
    if expected is not None and have > expected:
        part.unlink()
        have = 0

    headers = {"User-Agent": "project-watashi-fetch/0.1"}
    mode = "wb"
    if have:
        headers["Range"] = f"bytes={have}-"
        mode = "ab"
        print(f"    resuming {dest.name} at {human(have)}")

    try:
        with session.get(url, headers=headers, stream=True, timeout=60) as response:
            if have and response.status_code != 206:
                # server ignored the range; start over rather than corrupt
                have = 0
                mode = "wb"
            if response.status_code not in (200, 206):
                print(f"    FAILED {dest.name}: HTTP {response.status_code}")
                return False
            total = expected
            if total is None:
                length = response.headers.get("Content-Length")
                total = (int(length) + have) if length else None

            started = time.perf_counter()
            written = have
            last_report = 0.0
            with part.open(mode) as handle:
                for chunk in response.iter_content(chunk_size=1 << 20):
                    if not chunk:
                        continue
                    handle.write(chunk)
                    written += len(chunk)
                    now = time.perf_counter()
                    if now - last_report > 1.0:
                        last_report = now
                        speed = written / max(1e-6, now - started)
                        if total:
                            pct = written * 100.0 / total
                            sys.stdout.write(
                                f"\r    {dest.name:28s} {pct:5.1f}%  "
                                f"{human(written)} / {human(total)}  {human(speed)}/s   "
                            )
                        else:
                            sys.stdout.write(
                                f"\r    {dest.name:28s} {human(written)}  {human(speed)}/s   "
                            )
                        sys.stdout.flush()
            sys.stdout.write("\r" + " " * 78 + "\r")
            sys.stdout.flush()
    except KeyboardInterrupt:
        print(f"\n    interrupted; {dest.name} left resumable at {human(part.stat().st_size)}")
        raise
    except Exception as exc:
        print(f"    FAILED {dest.name}: {type(exc).__name__}: {exc}")
        return False

    final_size = part.stat().st_size
    if expected is not None and final_size != expected:
        print(f"    INCOMPLETE {dest.name}: {human(final_size)} of {human(expected)} (kept for resume)")
        return False

    if dest.exists():
        dest.unlink()
    part.replace(dest)
    print(f"    done     {dest.name:28s} {human(final_size)}")
    return True


def check(model_dir: Path) -> int:
    lock_status = verify_models_lock()
    print("")
    print(f"model directory: {model_dir}")
    if not model_dir.exists():
        print("  (does not exist)")
        return 1
    missing = []
    total = 0
    for name in MODEL_FILES:
        path = model_dir / name
        if path.exists():
            size = path.stat().st_size
            total += size
            print(f"  present  {name:28s} {human(size)}")
        else:
            missing.append(name)
            print(f"  MISSING  {name}")
    part_files = list(model_dir.glob("*.part"))
    for part in part_files:
        print(f"  partial  {part.name:28s} {human(part.stat().st_size)} (resumable)")
    print(f"  total {human(total)}")
    if missing:
        print(f"  {len(missing)} file(s) missing -> run without --check to fetch")
        return 1
    print("  model looks complete")
    return 0 if lock_status == 0 else 1


#: The files faster-whisper needs to open a CTranslate2 Whisper model. The
#: tokenizer and vocabulary names differ between converted repos, so only the
#: ones that are genuinely required are checked.
ASR_FILES = ("config.json", "model.bin", "tokenizer.json")


def asr_destination(args: argparse.Namespace) -> Path:
    if args.dest:
        return Path(args.dest).resolve()
    name = str(args.asr).rstrip("/").split("/")[-1]
    return (REPO_ROOT / "models" / "whisper" / name).resolve()


def fetch_asr(args: argparse.Namespace) -> int:
    """Download Whisper weights once, so watching a stream can be offline.

    Done through faster-whisper's own downloader rather than a hand-written file
    list: the file set differs between converted repositories, and guessing it
    is how a model ends up 99% present and unusable.
    """
    target = asr_destination(args)
    print("=" * 72)
    print("Project Watashi - local speech recognition model fetch")
    print("=" * 72)
    print(f"  model     : {args.asr}")
    print(f"  target    : {target}")
    print("  note      : one-time download; watching a stream then never uses the network")
    print("=" * 72)

    if args.check:
        lock_status = verify_models_lock()
        print("")
        missing = [name for name in ASR_FILES if not (target / name).exists()]
        for name in ASR_FILES:
            path = target / name
            size = human(path.stat().st_size) if path.exists() else "MISSING"
            print(f"  {name:28s} {size}")
        if missing:
            print(f"  {len(missing)} file(s) missing -> run again without --check")
            return 1
        print("  model looks complete")
        print("  set live.asr_model to this directory to load it without a download")
        return 0 if lock_status == 0 else 1

    try:
        from faster_whisper.utils import download_model  # type: ignore
    except Exception as exc:
        print("faster-whisper is required: pip install -r prototype/requirements.txt")
        print(f"  ({type(exc).__name__}: {exc})")
        return 2

    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        result = download_model(str(args.asr), output_dir=str(target), local_files_only=False)
    except Exception as exc:
        print(f"  FAILED: {type(exc).__name__}: {exc}")
        return 1
    print(f"  model ready in {result}")
    print("  set live.asr_model to this directory to load it without a download")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="fetch_model",
        description="Download a local translation model for Project Watashi",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--repo", default=DEFAULT_REPO, help="Hugging Face repo id")
    parser.add_argument("--name", default=DEFAULT_NAME, help="destination directory name")
    parser.add_argument("--dest", type=Path, default=None, help="explicit destination directory")
    parser.add_argument("--check", action="store_true", help="only report what is present")
    parser.add_argument("--force", action="store_true", help="re-download even if present")
    parser.add_argument(
        "--asr",
        nargs="?",
        const="small",
        default=None,
        metavar="SIZE_OR_ID",
        help="fetch a faster-whisper speech model instead (tiny/base/small/medium/"
             "large-v3, or a Hugging Face id). Default: small",
    )
    args = parser.parse_args(argv)

    if args.asr:
        return fetch_asr(args)

    model_dir = args.dest or (REPO_ROOT / "models" / "llm" / args.name)
    model_dir = Path(model_dir).resolve()

    if args.check:
        return check(model_dir)

    try:
        import requests
    except ImportError:
        print("requests is required: pip install requests")
        return 2

    base = f"https://huggingface.co/{args.repo}/resolve/main/"
    print("=" * 72)
    print("Project Watashi - local translation model fetch")
    print("=" * 72)
    print(f"  repo      : {args.repo}")
    print(f"  target    : {model_dir}")
    print("  note      : one-time download; the app itself never uses the network")
    print("=" * 72)

    failures: list[str] = []
    with requests.Session() as session:
        for name in MODEL_FILES:
            url = base + name
            expected = remote_size(session, url)
            label = human(expected) if expected else "unknown size"
            print(f"\n  {name}  ({label})")
            if not download(session, url, model_dir / name, expected, args.force):
                failures.append(name)

    print("")
    print("=" * 72)
    if failures:
        print(f"  {len(failures)} file(s) failed: {', '.join(failures)}")
        print("  re-run to resume; partial downloads are kept")
        return 1
    print(f"  model ready in {model_dir}")
    print("  set translation.nmt_model in config.yaml to this path to enable it")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
