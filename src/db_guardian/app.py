from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from db_guardian.config import load_settings
from db_guardian.container import Container, build_container
from db_guardian.backups.api import router as backups_router
from db_guardian.backups.recovery import recover_interrupted
from db_guardian.sources.api import router as sources_router


def create_app(container: Container, *, start_background: bool = True) -> FastAPI:
    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        if not start_background:
            yield
            return
        recover_interrupted(container.session_factory, container.settings.backups_dir)
        container.queue.start()
        try:
            yield
        finally:
            container.queue.stop()

    app = FastAPI(title="db-guardian", lifespan=lifespan)
    app.state.container = container

    if container.settings.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=container.settings.cors_origins,
            allow_methods=["*"],
            allow_headers=["*"],
        )

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    app.include_router(sources_router)
    app.include_router(backups_router)
    return app


def build() -> FastAPI:
    """Uvicorn factory: `uvicorn db_guardian.app:build --factory`."""
    return create_app(build_container(load_settings()))
