"""FastAPI web UI: waterfall + spectrum over WebSocket, REST controls."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from rfspectrum.bands import PRESETS
from rfspectrum.debrief import build_summary, debrief
from rfspectrum.runtime import DRONE_EMITTERS, Runtime

logger = logging.getLogger(__name__)
STATIC_DIR = Path(__file__).parent / "static"


class PresetIn(BaseModel):
    id: str


class ToggleIn(BaseModel):
    on: bool


class SimEmitterIn(BaseModel):
    id: str
    on: bool


class ScenarioIn(BaseModel):
    scenario: str


class BaselineIn(BaseModel):
    action: str = "set"  # set | clear


def create_app(runtime: Runtime) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        runtime.attach_loop(asyncio.get_running_loop())
        runtime.start()
        try:
            yield
        finally:
            runtime.stop()

    app = FastAPI(title="rf-spectrum", lifespan=lifespan)
    app.state.runtime = runtime

    @app.get("/", response_class=HTMLResponse)
    async def index():
        return (STATIC_DIR / "index.html").read_text()

    @app.get("/api/status")
    async def status():
        return runtime.status()

    @app.get("/api/presets")
    async def presets():
        return [dict(p.to_dict(), available=runtime.preset_available(p.id)) for p in PRESETS.values()]

    @app.post("/api/preset")
    async def set_preset(body: PresetIn):
        if body.id not in PRESETS:
            raise HTTPException(404, f"unknown preset {body.id}")
        runtime.set_preset(body.id)
        runtime.stop_drone_watch()  # the presenter picked a band
        if runtime.sim is not None and runtime.sim.scenario == "auto":
            runtime.sim.scenario = "manual"  # the presenter has taken over
        return {"preset": runtime.preset_id, "available": runtime.preset_available(body.id)}

    @app.post("/api/scan")
    async def scan(body: ToggleIn):
        runtime.set_scan(body.on)
        return {"scan": runtime.scan}

    @app.get("/api/events")
    async def events(limit: int = 200):
        return runtime.engine.get_events(limit)

    @app.get("/api/emitters")
    async def emitters():
        return runtime.engine.get_emitters()

    @app.get("/api/emcon")
    async def emcon():
        return runtime.engine.get_emcon()

    @app.post("/api/baseline")
    async def baseline(body: BaselineIn):
        if body.action == "clear":
            runtime.engine.clear_baseline()
        else:
            runtime.engine.set_baseline()
        return {"baseline": len(runtime.engine.baseline)}

    @app.post("/api/reset")
    async def reset():
        runtime.engine.reset()
        return {"ok": True}

    @app.post("/api/sim/emitter")
    async def sim_emitter(body: SimEmitterIn):
        if runtime.sim is None:
            raise HTTPException(409, "not in simulate mode")
        try:
            runtime.sim.set_emitter(body.id, body.on, time.time())
        except KeyError:
            raise HTTPException(404, f"unknown simulated emitter {body.id}") from None
        if body.id in DRONE_EMITTERS:
            if body.on:
                runtime.start_drone_watch()  # sweep the bands the drone link uses
            elif not any(runtime.sim.emitters[d].enabled for d in DRONE_EMITTERS):
                runtime.stop_drone_watch()
        return runtime.sim.state()

    @app.post("/api/sim/scenario")
    async def sim_scenario(body: ScenarioIn):
        if runtime.sim is None:
            raise HTTPException(409, "not in simulate mode")
        if body.scenario not in ("auto", "manual"):
            raise HTTPException(400, "scenario must be auto or manual")
        runtime.sim.set_scenario(body.scenario, time.time())
        return runtime.sim.state()

    @app.post("/api/debrief")
    def make_debrief():  # sync: runs in a worker thread (Gemini call may block)
        summary = build_summary(runtime.engine.get_emitters(), runtime.engine.get_emcon(), runtime.rules)
        return debrief(summary, runtime.settings.gemini_api_key, runtime.settings.gemini_model)

    @app.websocket("/ws")
    async def ws(websocket: WebSocket):
        await websocket.accept()
        q = runtime.subscribe()
        try:
            await websocket.send_text(json.dumps(runtime.status()))
            last_status = time.monotonic()
            while True:
                try:
                    msg = await asyncio.wait_for(q.get(), timeout=1.0)
                    await websocket.send_text(msg)
                except asyncio.TimeoutError:
                    pass
                if time.monotonic() - last_status >= 1.0:
                    await websocket.send_text(json.dumps(runtime.status()))
                    last_status = time.monotonic()
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            runtime.unsubscribe(q)

    return app
