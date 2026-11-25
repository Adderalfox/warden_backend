import sys
import asyncio

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

if sys.platform == "win32":
    import uvicorn.loops.asyncio
    uvicorn.loops.asyncio.asyncio_setup = lambda use_subprocess=False: None
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())



from app.api.v1 import alerts, approval, incidents, metrics
from app.metrics.service import metrics_service
from app.mcp_clients.manager import mcp_manager


@asynccontextmanager
async def lifespan(app: FastAPI):
    await metrics_service.start()
    yield
    await metrics_service.stop()
    await mcp_manager.aclose()


app = FastAPI(title="Warden", version="0.2.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://127.0.0.1:3000"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(alerts.router)
app.include_router(incidents.router)
app.include_router(approval.router)
app.include_router(metrics.router)


@app.get("/health")
async def health():
    return {"status": "ok", "service": "warden-backend"}
