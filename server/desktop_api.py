"""仅供桌面宿主使用的本地鉴权 API。"""

import json
import secrets
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from server.desktop_bridge import BridgeError
from server.desktop_models import JobRequest, PageRequest, Settings, Target


class CookiesRequest(BaseModel):
    cookies: dict = Field(default_factory=dict)


class CaptureRequest(BaseModel):
    text: str = Field(max_length=10_000_000)


def build_desktop_app(runtime, token):
    @asynccontextmanager
    async def lifespan(app):
        await runtime.open()
        yield
        await runtime.close()

    app = FastAPI(title="Douyin Desktop", lifespan=lifespan, docs_url=None, redoc_url=None)
    app.state.runtime = runtime

    @app.middleware("http")
    async def authenticate(request: Request, call_next):
        if not secrets.compare_digest(request.headers.get("authorization", ""), "Bearer " + token):
            return JSONResponse({"detail": "本地连接认证失败"}, status_code=401)
        return await call_next(request)

    @app.exception_handler(BridgeError)
    async def bridge_error(request, exc):
        return JSONResponse({"detail": str(exc), "code": exc.page_bridge_code}, status_code=409)

    @app.exception_handler(ValueError)
    async def invalid(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=400)

    @app.exception_handler(OSError)
    async def filesystem_error(request, exc):
        return JSONResponse({"detail": "文件操作失败，请检查目录权限、可用空间或文件是否存在"}, status_code=400)

    @app.get("/api/v1/health")
    async def health():
        return {"status": "ok"}

    @app.get("/api/v1/settings")
    async def settings():
        return runtime.settings()

    @app.put("/api/v1/settings")
    async def save_settings(body: Settings):
        return await runtime.save_settings(body)

    @app.post("/api/v1/session")
    async def session(body: CookiesRequest):
        return await runtime.session(body.cookies)

    @app.get("/api/v1/me")
    async def me():
        return await runtime.me()

    @app.get("/api/v1/diagnostics")
    async def diagnostics():
        return await runtime.diagnostics()

    @app.post("/api/v1/capture")
    async def capture(body: CaptureRequest):
        try:
            payload = json.loads(body.text)
        except ValueError as exc:
            raise ValueError("页面数据不是有效 JSON") from exc
        return {"items": runtime.cache.ingest(payload)}

    @app.post("/api/v1/content")
    async def content(body: PageRequest):
        return await runtime.page(body.target, body.cursor)

    @app.post("/api/v1/import")
    async def import_data(body: dict):
        return await runtime.import_data(body.get("path", ""))

    @app.post("/api/v1/jobs")
    async def create_job(body: JobRequest):
        return await runtime.submit(body)

    @app.post("/api/v1/download")
    async def legacy_download(body: dict):
        job = await runtime.submit(JobRequest(target=Target(kind="url", url=body.get("url", ""))))
        return {"job_id": job["job_id"], "status": job["status"], "url": body["url"]}

    @app.get("/api/v1/jobs")
    async def jobs():
        return {"jobs": list(reversed(list(runtime.jobs.values())))}

    @app.get("/api/v1/jobs/{key}")
    async def job(key: str):
        if key not in runtime.jobs:
            raise HTTPException(404, "任务不存在")
        return runtime.jobs[key]

    @app.post("/api/v1/jobs/{key}/{action}")
    async def action(key: str, action: str):
        await job(key)
        if action == "cancel":
            return await runtime.cancel(key)
        if action in {"resume", "retry"}:
            return await runtime.resume(key)
        raise HTTPException(404, "操作不存在")

    @app.get("/api/v1/library")
    async def library(page: int = Query(1, ge=1), title: str = "", author: str = "",
                      date_from: int = 0, date_to: int = 0):
        return await runtime.library(page=page, size=40, title=title, author=author,
                                     date_from=date_from or None, date_to=date_to or None)

    @app.websocket("/api/v1/desktop/session")
    async def socket(websocket: WebSocket):
        protocols = websocket.headers.get("sec-websocket-protocol", "").split(",")
        credentials = [x.strip() for x in protocols]
        if not any(secrets.compare_digest(x, token) for x in credentials):
            await websocket.close(code=1008)
            return
        if runtime.bridge.socket is not None:
            await websocket.close(code=1008)
            return
        await websocket.accept(subprotocol=token)
        runtime.bridge.socket = websocket
        try:
            await runtime.bridge.send({"type": "connected"})
            while True:
                message = await websocket.receive_json()
                if message.get("type") == "response":
                    runtime.bridge.answer(message)
        except WebSocketDisconnect:
            pass
        finally:
            runtime.bridge.disconnect()

    return app
