"""The FastAPI application: `create_app` wires the routers, the loopback guard and the watcher."""

import asyncio
import contextlib
import functools
import sqlite3
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime

import httpx
from fastapi import APIRouter, FastAPI

import vigil
from vigil.config import Config
from vigil.hub import HUB_DIR, HubStore
from vigil.index.db import connect
from vigil.index.scanner import ScanStats
from vigil.paths import data_dir, index_path
from vigil.peers import CACHE_DIR, PeerHub
from vigil.review.runner import STOPPED
from vigil.review.store import fail_if_running, fail_running
from vigil.web import WebState, api, auth, pages, review, watcher
from vigil.web.security import LoopbackGuard

# Append point: include a router by appending one line.
ROUTERS: list[APIRouter] = [
    api.router,
    api.actions,
    pages.router,
    auth.router,
    review.router,
]


def _no_index() -> sqlite3.Connection:
    """A hub's index connection factory: a hub has no index."""
    raise RuntimeError("hub mode has no index")


def create_app(
    config: Config,
    *,
    conn_factory: Callable[[], sqlite3.Connection] | None = None,
    start_watcher: bool = True,
    peer_transport: httpx.BaseTransport | None = None,
    access_transport: httpx.BaseTransport | None = None,
) -> FastAPI:
    """The vigil web service.

    `conn_factory` opens an index connection (default: the index in the data dir); handlers open
    one per request and close it. With `start_watcher`, the app scans the transcripts under
    `config.claude_home` on startup and keeps the index current while it runs (`watcher.run`).
    Every request passes `LoopbackGuard` first. With `config.server.token_file` set, remote
    access is on: the token is read and the cookie signing key loaded (or created) in the data
    dir now, and AuthError is raised when either cannot be used (`vigil.web.auth.load_auth`).
    `config.peers` are asked for their sessions when a page lists them (`vigil.peers`);
    `peer_transport` replaces the network for those requests (tests). On startup, reviews left
    `running` by an earlier run of the service are marked failed (`vigil.review.store`); on
    shutdown, a review in progress has its `claude` stopped and is marked failed.

    In hub mode (`config.mode == "hub"`) there is no index (`conn_factory` defaults to one that
    raises RuntimeError), no watcher and no review, and nothing runs `claude`; `start_watcher`
    is ignored. The machines' snapshots (`vigil.hub.HubStore`, in the data dir's `hub/`) are
    read on startup, and when each was last seen is written on shutdown.
    """
    remote = auth.load_auth(config.server, data_dir(), access_transport=access_transport)
    is_hub = config.mode == "hub"
    factory = conn_factory or (_no_index if is_hub else functools.partial(connect, index_path()))
    hub = PeerHub(config.peers, cache_dir=data_dir() / CACHE_DIR, transport=peer_transport)
    web = WebState(config=config, conn_factory=factory, peers=hub)
    if is_hub:
        web.hub = HubStore(data_dir() / HUB_DIR, config.machines, config.hub_mode.stale_after)

    def scanned(stats: ScanStats) -> None:
        web.last_scan_at = datetime.now(UTC)

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if web.hub is not None:
            store = web.hub
            await asyncio.to_thread(store.load)
            try:
                yield
            finally:
                await asyncio.to_thread(store.flush, force=True)
            return
        # A review still marked running was cut off when the service last stopped.
        conn = factory()
        try:
            fail_running(conn)
        finally:
            conn.close()
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
            # End a review's `claude` rather than leave it running without the service.
            stopped = await asyncio.to_thread(web.review_slot.stop)
            if stopped is not None:
                conn = factory()
                try:
                    fail_if_running(conn, stopped, STOPPED)
                finally:
                    conn.close()

    app = FastAPI(title="vigil", version=vigil.__version__, lifespan=lifespan)
    app.state.vigil = web
    app.state.vigil_auth = remote
    for router in ROUTERS:
        app.include_router(router)
    app.add_middleware(LoopbackGuard, auth=remote)
    return app
