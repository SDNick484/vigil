"""The FastAPI application: `create_app` wires the routers, the loopback guard and the watcher."""

import asyncio
import contextlib
import functools
import sqlite3
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime

from fastapi import APIRouter, FastAPI

import vigil
from vigil.config import Config
from vigil.index.db import connect
from vigil.index.scanner import ScanStats
from vigil.paths import index_path
from vigil.web import WebState, api, watcher
from vigil.web.security import LoopbackGuard

# Append point: include a router by appending one line.
ROUTERS: list[APIRouter] = [
    api.router,
    api.actions,
]


def create_app(
    config: Config,
    *,
    conn_factory: Callable[[], sqlite3.Connection] | None = None,
    start_watcher: bool = True,
) -> FastAPI:
    """The vigil web service.

    `conn_factory` opens an index connection (default: the index in the data dir); handlers open
    one per request and close it. With `start_watcher`, the app scans the transcripts under
    `config.claude_home` on startup and keeps the index current while it runs (`watcher.run`).
    Every request passes `LoopbackGuard` first.
    """
    factory = conn_factory or functools.partial(connect, index_path())
    web = WebState(config=config, conn_factory=factory)

    def scanned(stats: ScanStats) -> None:
        web.last_scan_at = datetime.now(UTC)

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        task: asyncio.Task[None] | None = None
        if start_watcher:
            task = asyncio.create_task(
                watcher.run(
                    factory, config.claude_home, on_scan=scanned, on_ready=web.watcher_ready.set
                ),
                name="vigil-watcher",
            )
        try:
            yield
        finally:
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task

    app = FastAPI(title="vigil", version=vigil.__version__, lifespan=lifespan)
    app.state.vigil = web
    for router in ROUTERS:
        app.include_router(router)
    app.add_middleware(LoopbackGuard)
    return app
