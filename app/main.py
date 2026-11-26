import sys
import asyncio

from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware

if sys.platform == "win32":
    import uvicorn.loops.asyncio
    uvicorn.loops.asyncio.asyncio_setup = lambda use_subprocess=False: None
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())



from app.api.v1 import alerts, analytics, approval, assistant, auth, incidents, metrics
from app.auth.deps import current_user
from app.auth.security import DEFAULT_SECRET
from app.core.config import settings
from app.metrics.service import metrics_service
from app.mcp_clients.manager import mcp_manager


@asynccontextmanager
async def lifespan(app: FastAPI):
    if settings.auth_required and settings.environment == "production" and settings.auth_secret == DEFAULT_SECRET:
        raise RuntimeError("WARDEN_AUTH_SECRET must be set to a long random value when WARDEN_ENVIRONMENT=production")
    await metrics_service.start()
    yield
    await metrics_service.stop()
    await mcp_manager.aclose()


app = FastAPI(title="Warden", version="0.3.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://127.0.0.1:3000"],
    allow_credentials=True,   # session cookie on the incident SSE stream
    allow_methods=["*"],
    allow_headers=["*"],
)

protected = [Depends(current_user)]

# Machine-to-machine endpoints keep their own credentials (HMAC signature / approval API key).
app.include_router(alerts.router)
app.include_router(approval.router)

# Login / sign-up are public; everything the dashboard reads requires a session.
app.include_router(auth.router)
app.include_router(incidents.router, dependencies=protected)
app.include_router(metrics.router, dependencies=protected)
app.include_router(analytics.router, dependencies=protected)
app.include_router(assistant.router, dependencies=protected)


@app.get("/health")
async def health():
    return {"status": "ok", "service": "warden-backend"}
