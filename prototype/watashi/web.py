"""Local web panel: a settings editor and a read-only view.

Scope, deliberately narrow: the panel exists to **adjust settings** and to
**view** what the engine is doing. It does not implement features. Concretely:

* **Settings (write).** The presentation spec, the active profile, the target
  language, fps and the change-detection threshold. Applying a setting from a
  settings panel is the whole point of the panel.
* **Views (read only).** Live subtitles, bilingual history, counters, the loaded
  corpus and rules, and the raw event stream.
* **Not implemented here.** Corpus authoring, and runtime transport controls such
  as pause. Corpora are files on disk and hot reload already picks up edits by
  modification time, so a reload button would be redundant; and pausing the
  engine is a control, not a setting.

The boundary is enforced **server side**, not by omitting buttons: post a
non-settings command and it is rejected with an explanation. A boundary that
only exists in the UI is one stray fetch away from being broken.

Other constraints that shaped this:

* **Same process as the engine.** The panel is mounted *inside* the running
  application so it shares the loaded OCR engine, corpus and model. Spawning a
  second process would duplicate 715 MiB of model weights for no reason.
* **Loopback by default.** The project's security section asks for local-only
  processing and minimal permissions, so the panel binds ``127.0.0.1`` unless the
  operator explicitly asks for more.
* **Zero new dependencies and zero network fetches.** FastAPI and uvicorn are
  already present, and the page inlines all CSS and JS -- a CDN reference would
  break the offline guarantee that requirement R1 is about.
* **SSE, not WebSocket.** Subtitle delivery is one-directional server -> client,
  so Server-Sent Events do the job over plain HTTP and avoid needing a
  ``websockets`` dependency (which is not installed).

The panel is not the primary display. A browser tab costs 150-400 MiB, which is
25-65x the native overlay's 6 MiB, so the memory-lean path remains the CLI and
the floating overlay.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from pathlib import Path
from typing import Any, AsyncIterator

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse

from .config import AppConfig
from .events import (
    CMD_LOAD_PROFILE,
    CMD_SET_DIFF_THRESHOLD,
    CMD_SET_FPS,
    CMD_SET_PRESENTATION,
    CMD_SET_REGION,
    CMD_SET_TARGET_LANG,
    CMD_STATUS,
    SCHEMA_VERSION,
    decode_command,
)
from .session import Session

_INDEX = Path(__file__).resolve().parent.parent / "web" / "index.html"

#: Commands the panel may issue. Everything else is a feature, not a setting, and
#: belongs to the CLI or the engine itself.
#:
#: Deliberately absent: ``pause``/``resume``/``toggle_pause`` (runtime control),
#: ``reload_corpus`` (hot reload already happens on mtime), ``shutdown`` (not a
#: setting and too easy to hit by accident).
SETTINGS_COMMANDS: tuple[str, ...] = (
    CMD_SET_PRESENTATION,
    CMD_LOAD_PROFILE,
    CMD_SET_TARGET_LANG,
    CMD_SET_FPS,
    CMD_SET_DIFF_THRESHOLD,
    CMD_SET_REGION,
    CMD_STATUS,
)

#: Directives that disable every remote fetch, so the page cannot phone home
#: even by accident.
_CSP = (
    "default-src 'none'; "
    "style-src 'unsafe-inline'; "
    "script-src 'unsafe-inline'; "
    "connect-src 'self'; "
    "img-src 'self' data:; "
    "form-action 'none'; "
    "base-uri 'none'"
)


def _command_payload(field: Any, raw: Any) -> dict[str, Any]:
    """Translate a settings value into the payload its command expects.

    The engine's commands take their argument under different names because they
    grew up separately (`value`, `target_lang`, `preset`, `region`). Rather than
    rename them all and break every existing caller, the mapping lives here, next to
    the only place that needs it.
    """
    from . import settings_schema

    value = settings_schema.coerce(field, raw)
    name = field.command
    if name in ("set_fps", "set_diff_threshold"):
        return {"value": value}
    if name == "set_target_lang":
        return {"target_lang": value}
    if name == "set_presentation":
        return {"preset": value}
    if name == "set_region":
        return {"region": value}
    if name == "load_profile":
        return {"name": value}
    return {"value": value}


def create_app(session: Session) -> FastAPI:
    """Build the panel's ASGI app around a live session."""
    app = FastAPI(
        title="Project Watashi",
        version=str(SCHEMA_VERSION),
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.session = session

    @app.get("/", response_class=HTMLResponse)
    async def index() -> HTMLResponse:
        if not _INDEX.exists():
            return HTMLResponse(
                "<h1>Project Watashi</h1><p>web/index.html is missing.</p>",
                status_code=500,
            )
        return HTMLResponse(
            _INDEX.read_text(encoding="utf-8"),
            headers={"Content-Security-Policy": _CSP, "Cache-Control": "no-store"},
        )

    @app.get("/api/info")
    async def api_info() -> JSONResponse:
        data = session.info()
        data["settings_commands"] = list(SETTINGS_COMMANDS)
        return JSONResponse(data)

    @app.get("/api/stats")
    async def api_stats() -> JSONResponse:
        return JSONResponse(session.stats_dict())

    @app.get("/api/corpus")
    async def api_corpus() -> JSONResponse:
        """Read-only: what the engine currently has loaded.

        There is no write counterpart. Corpora are files; edit them and the
        engine's mtime hot reload picks it up.
        """
        corpus = session.corpus
        if corpus is None:
            return JSONResponse({"entries": [], "rules": [], "size": 0})
        entries = [
            {
                "source": entry.source,
                "target": entry.target,
                "layer": entry.layer,
                "origin": entry.origin,
                "priority": entry.priority,
            }
            for entry in corpus.entries_snapshot()
        ]
        return JSONResponse(
            {"size": corpus.size, "rules": corpus.rule_ids(), "entries": entries}
        )

    @app.post("/api/command")
    async def api_command(request: Request) -> JSONResponse:
        """Apply a **setting**. Feature commands are refused here."""
        body: Any = await request.json()
        if not isinstance(body, dict):
            raise HTTPException(status_code=400, detail="command must be a JSON object")
        try:
            name, payload = decode_command(body)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        if name not in SETTINGS_COMMANDS:
            raise HTTPException(
                status_code=403,
                detail=(
                    f"{name!r} is an engine operation, not a setting, so the panel "
                    f"does not issue it. This surface is for adjusting settings and "
                    f"viewing state; use the CLI for {name!r}. "
                    f"Allowed here: {', '.join(SETTINGS_COMMANDS)}"
                ),
            )

        result = session.command(name, payload)
        return JSONResponse(result, status_code=200 if result.get("ok") else 400)

    @app.get("/api/settings")
    async def api_settings() -> JSONResponse:
        """The whole settings schema, with current values, for a form to render.

        Every field carries its own label and description, so the page needs no
        knowledge of any individual setting: adding a field to the schema makes it
        appear here, with its explanation, without touching the HTML.
        """
        from . import settings_schema

        payload = settings_schema.as_dict(session.config)
        overrides = session.config.read_overrides()
        # Which values the user has changed, so the page can mark them and offer a
        # reset. Without this the panel cannot tell a deliberate choice from a
        # shipped default.
        changed = []
        for section, values in overrides.items():
            if isinstance(values, dict):
                changed.extend(f"{section}.{name}" for name in values)
            else:
                changed.append(section)
        payload["overridden"] = sorted(changed)
        payload["config_file"] = str(session.config.base_dir / "config.yaml")
        payload["overrides_file"] = str(
            AppConfig.overrides_path(session.config.base_dir)
        )
        return JSONResponse(payload)

    @app.post("/api/settings")
    async def api_set_settings(request: Request) -> JSONResponse:
        """Validate, persist and broadcast settings.

        All of that is ``session.apply_settings``, shared with the desktop window, so
        a change made here reaches the other surface and vice versa. This handler only
        translates HTTP into that call -- duplicating the logic here is how the two
        surfaces would start disagreeing about what is saved.
        """
        body: Any = await request.json()
        if not isinstance(body, dict):
            raise HTTPException(status_code=400, detail="expected a JSON object")
        changes = body.get("changes")
        if not isinstance(changes, dict) or not changes:
            raise HTTPException(status_code=400, detail="expected a non-empty 'changes' object")

        result = session.apply_settings(changes)
        if not result.get("ok"):
            # A rejected value must say which one and why; a settings page that
            # silently accepts or clamps input teaches the user its controls lie.
            return JSONResponse(result, status_code=400)
        return JSONResponse(result)

    @app.get("/api/events")
    async def api_events(request: Request, replay: int = 0) -> StreamingResponse:
        channel = session.subscribe()
        # a fresh subscriber needs the current state immediately, otherwise the
        # panel sits empty until the next subtitle happens to arrive
        await asyncio.to_thread(_prime, channel, session)

        async def stream() -> AsyncIterator[bytes]:
            try:
                while True:
                    if await request.is_disconnected():
                        break
                    try:
                        envelope = channel.get_nowait()
                    except Exception:
                        yield b": keepalive\n\n"
                        await asyncio.sleep(0.15)
                        continue
                    payload = json.dumps(envelope, ensure_ascii=False)
                    event_type = str(envelope.get("type", "message"))
                    seq = envelope.get("seq", "")
                    yield (
                        f"id: {seq}\nevent: {event_type}\ndata: {payload}\n\n".encode("utf-8")
                    )
            finally:
                session.unsubscribe(channel)

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-store",
                "X-Accel-Buffering": "no",
                "Connection": "keep-alive",
            },
        )

    return app


def _prime(channel: Any, session: Session) -> None:
    """Push a synthetic ready+stats burst so a new client renders immediately."""
    from .events import EVENT_READY, EVENT_STATS, encode_event, encode_stats

    try:
        channel.put_nowait(encode_event(EVENT_READY, session.info(), seq=0))
        channel.put_nowait(
            encode_event(EVENT_STATS, encode_stats(session.stats()), seq=0, timestamp=time.time())
        )
    except Exception:
        pass


# --------------------------------------------------------------------------- #
# lifecycle
# --------------------------------------------------------------------------- #


class WebPanel:
    """Runs uvicorn on a background thread so Tk can own the main thread.

    Running the server in a thread rather than the main thread matters when the
    floating overlay is also active: Tk requires the main thread, so the panel
    has to be the one that moves.
    """

    def __init__(
        self,
        session: Session,
        host: str = "127.0.0.1",
        port: int = 8765,
        log_level: str = "warning",
    ) -> None:
        self.session = session
        self.host = host
        self.port = port
        self.log_level = log_level
        self.app = create_app(session)
        self._server: Any = None
        self._thread: threading.Thread | None = None
        self.started_at = 0.0

    @property
    def url(self) -> str:
        host = "127.0.0.1" if self.host in ("0.0.0.0", "::") else self.host
        return f"http://{host}:{self.port}/"

    @property
    def exposed(self) -> bool:
        """True when bound to something other than loopback."""
        return self.host not in ("127.0.0.1", "localhost", "::1")

    def start(self) -> bool:
        """Launch the server thread. Readiness is ``wait_until_ready``.

        Refuses to start a second server on top of a live one. Two servers sharing a
        port is not a race that resolves itself: the second fails to bind, its thread
        exits, and the first quietly keeps serving -- with the *old* session attached,
        so requests appear to succeed while reading stale state.
        """
        if self._thread is not None and self._thread.is_alive():
            print("[web] refusing to start: a server thread is already running")
            return False
        try:
            import uvicorn
        except ImportError:
            print("[web] uvicorn is not installed; run: pip install uvicorn")
            return False

        config = uvicorn.Config(
            self.app,
            host=self.host,
            port=self.port,
            log_level=self.log_level,
            access_log=False,
        )
        self._server = uvicorn.Server(config)
        self._thread = threading.Thread(
            target=self._server.run, name="watashi-web", daemon=True
        )
        self._thread.start()
        self.started_at = time.perf_counter()
        return True

    def wait_until_ready(self, timeout: float = 10.0) -> bool:
        deadline = time.perf_counter() + timeout
        while time.perf_counter() < deadline:
            if self._server is not None and getattr(self._server, "started", False):
                return True
            if self._thread is not None and not self._thread.is_alive():
                return False
            time.sleep(0.05)
        return False

    def stop(self, timeout: float = 5.0) -> bool:
        """Shut the server down and **confirm** it stopped. Returns whether it did.

        The old version set ``should_exit``, joined once, and cleared its handles
        regardless of the outcome. When the join timed out -- which is what an open
        SSE stream causes, because uvicorn's graceful shutdown waits for in-flight
        connections -- the panel reported itself stopped while the thread kept
        running and kept the port bound. The next ``start()`` then failed to bind and
        every request went to the zombie, whose session was a different one. That is
        the flakiness: a refused connection at one moment, an aborted one at another,
        on whichever line happened to be running.

        So: escalate to ``force_exit`` (uvicorn's "abandon the connections") and join
        again, and only clear the handles once the thread is genuinely gone.
        """
        stopped = True
        if self._server is not None:
            self._server.should_exit = True
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            if self._thread.is_alive():
                # graceful shutdown is not going to finish; abandon the connections
                if self._server is not None:
                    self._server.force_exit = True
                self._thread.join(timeout=timeout)
            if self._thread.is_alive():
                stopped = False
                print(
                    "[web] warning: the server thread did not stop within "
                    f"{timeout * 2:.0f}s; port {self.port} may still be held"
                )
        if stopped:
            self._server = None
            self._thread = None
        return stopped
