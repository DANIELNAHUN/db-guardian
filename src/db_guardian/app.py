from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from db_guardian.config import load_settings
from db_guardian.container import Container, build_container


def create_app(container: Container, *, start_background: bool = True) -> FastAPI:
    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        yield

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

    return app


def build() -> FastAPI:
    """Uvicorn factory: `uvicorn db_guardian.app:build --factory`."""
    return create_app(build_container(load_settings()))
