import asyncio
import hmac
import logging
import time
import uuid
from contextlib import asynccontextmanager
from typing import Annotated, Literal

from fastapi import Depends, FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from app.agent import Agent
from app.config import ASSET_ROOT, Settings
from app.knowledge import Knowledge
from app.providers import ProviderError, Providers
from app.store import Store
from app.tools import Tools

ROOT = ASSET_ROOT
logger = logging.getLogger("uvicorn.error.manualpilot")


class HistoryMessage(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(min_length=1, max_length=4000)


class ChatRequest(BaseModel):
    question: str = Field(min_length=1, max_length=2000, pattern=r"\S")
    history: list[HistoryMessage] = Field(default_factory=list, max_length=6)
    allow_control: bool = False
    top_k: int | None = Field(default=None, ge=1, le=12)


class SearchRequest(BaseModel):
    query: str = Field(min_length=1, max_length=2000, pattern=r"\S")
    top_k: int = Field(default=4, ge=1, le=12)


def create_app(settings: Settings | None = None, providers: Providers | None = None) -> FastAPI:
    cfg = settings or Settings()

    @asynccontextmanager
    async def lifespan(application):
        store = Store(cfg)
        upstream = providers or Providers(cfg)
        knowledge = Knowledge(cfg, store, upstream)
        tools = Tools(cfg)
        application.state.settings = cfg
        application.state.store = store
        application.state.knowledge = knowledge
        application.state.providers = upstream
        application.state.tools = tools
        application.state.agent = Agent(cfg, knowledge, upstream, tools)
        application.state.chat_slots = asyncio.Semaphore(8)
        try:
            if cfg.seed_demo:
                await asyncio.to_thread(tools.seed)
                # Seed only once: a user's deliberate deletion must survive restart.
                marker = cfg.data_dir / ".demo-seeded"
                if not marker.exists():
                    sample = ROOT / "examples" / "demo_manual.md"
                    await knowledge.ingest(sample.name, sample.read_bytes())
                    marker.touch()
            yield
        finally:
            await upstream.close()

    application = FastAPI(title="ManualPilot 文档与设备助手", version="1.0.0", lifespan=lifespan)

    @application.middleware("http")
    async def request_metadata(request: Request, call_next):
        request.state.request_id = uuid.uuid4().hex
        start = time.perf_counter()
        # Reject known oversized bodies before multipart parsing. The streaming guard below
        # also covers chunked requests and dishonest Content-Length headers.
        limit = (cfg.max_upload_mb + 1) * 1024 * 1024
        if request.url.path.startswith("/api/"):
            if cfg.api_token and not hmac.compare_digest(
                request.headers.get("authorization", ""),
                f"Bearer {cfg.api_token}",
            ):
                return JSONResponse({"detail": "访问令牌无效"}, status_code=401)
            received = 0
            original_receive = request._receive

            async def limited_receive():
                nonlocal received
                message = await original_receive()
                received += len(message.get("body", b""))
                if received > limit:
                    raise HTTPException(413, "请求体超过大小限制")
                return message

            request._receive = limited_receive
            raw_length = request.headers.get("content-length")
            if raw_length and (not raw_length.isdigit() or int(raw_length) > limit):
                return JSONResponse({"detail": "请求体超过大小限制"}, status_code=413)
        response = await call_next(request)
        response.headers["X-Request-ID"] = request.state.request_id
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'"
        )
        logger.info(
            "%s %s status=%s duration_ms=%.2f request_id=%s",
            request.method,
            request.url.path,
            response.status_code,
            (time.perf_counter() - start) * 1000,
            request.state.request_id,
        )
        return response

    async def authorize(request: Request):
        # Declare the security requirement on API routes as well as enforce it before parsing.
        if cfg.api_token and not hmac.compare_digest(
            request.headers.get("authorization", ""),
            f"Bearer {cfg.api_token}",
        ):
            raise HTTPException(401, "访问令牌无效")

    auth = [Depends(authorize)]

    @application.exception_handler(ProviderError)
    async def provider_error(request, error):
        return JSONResponse(
            {"detail": str(error), "request_id": request.state.request_id}, status_code=502
        )

    @application.get("/healthz")
    async def health():
        return {"status": "ok"}

    @application.get("/api/status", dependencies=auth)
    async def status(request: Request):
        return {
            "llm_mode": cfg.llm_mode,
            "embedding_mode": cfg.embedding_mode,
            "serial_mode": cfg.serial_mode,
            "model": cfg.llm_model if cfg.llm_mode == "api" else "offline-demo",
            "max_upload_mb": cfg.max_upload_mb,
            **await asyncio.to_thread(request.app.state.store.stats),
        }

    @application.get("/api/documents", dependencies=auth)
    async def documents(request: Request):
        return await asyncio.to_thread(request.app.state.store.list_documents)

    @application.post("/api/documents", dependencies=auth)
    async def upload(request: Request, file: Annotated[UploadFile, File()]):
        try:
            content = await file.read(cfg.max_upload_mb * 1024 * 1024 + 1)
            if len(content) > cfg.max_upload_mb * 1024 * 1024:
                raise HTTPException(413, "文件超过上传大小限制")
            return await request.app.state.knowledge.ingest(file.filename or "", content)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        finally:
            await file.close()

    @application.delete("/api/documents/{document_id}", dependencies=auth)
    async def delete_document(document_id: str, request: Request):
        if not await request.app.state.knowledge.delete(document_id):
            raise HTTPException(404, "文档不存在")
        return {"deleted": document_id}

    @application.post("/api/search", dependencies=auth)
    async def search(body: SearchRequest, request: Request):
        return {"sources": await request.app.state.knowledge.search(body.query, body.top_k)}

    @application.post("/api/chat", dependencies=auth)
    async def chat(body: ChatRequest, request: Request):
        slots = request.app.state.chat_slots
        try:
            await asyncio.wait_for(slots.acquire(), timeout=0.1)
        except TimeoutError as exc:
            raise HTTPException(429, "当前问答繁忙，请稍后重试") from exc
        try:
            result = await request.app.state.agent.answer(body)
            return {**result, "request_id": request.state.request_id}
        finally:
            slots.release()

    application.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")

    @application.get("/", include_in_schema=False)
    async def home():
        return FileResponse(ROOT / "static" / "index.html")

    return application


app = create_app()
