from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from backend.api.routers import router
from backend.config import get_settings
from backend.infrastructure.bootstrap import bootstrap
from backend.infrastructure.mcp import close_mcp_clients
from backend.runtime.agent_executor import close_subagent_executor
from backend.services.interrupt_broadcast import interrupt_broadcast


@asynccontextmanager
async def lifespan(_app: FastAPI):
    bootstrap()
    interrupt_broadcast.start()
    try:
        yield
    finally:
        interrupt_broadcast.stop()
        close_subagent_executor()
        close_mcp_clients()


app = FastAPI(title="GoGo Agent Python API", version="1.0.0", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])
app.include_router(router)

@app.get("/health")
def health():
    from backend.runtime.agent_executor import get_subagent_executor
    executor = get_subagent_executor()
    payload = {"status": "ok", "runtime": "python", "workflow": "langgraph",
               "execution_mode": get_settings().subagent_execution_mode,
               "cluster_mode": get_settings().cluster_mode,
               "interrupt_listener": interrupt_broadcast.running}
    if hasattr(executor, "health"):
        payload["workers"] = executor.health()
    return payload
