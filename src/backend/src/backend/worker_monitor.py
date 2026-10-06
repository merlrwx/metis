import asyncio
import contextlib
import os
import socket

from prometheus_client import start_http_server
from redis.asyncio import Redis
from redis.exceptions import RedisError

from backend import observability
from backend.queue import REDIS_URL


async def start(state):
    observability.configure_logging()
    port = int(os.environ.get("METIS_WORKER_METRICS_PORT", "0"))
    if port:
        state.metrics_server, state.metrics_thread = start_http_server(port)
    state.heartbeat_key = f"metis:worker:{socket.gethostname()}:{os.getpid()}"
    state.heartbeat_redis = Redis.from_url(
        REDIS_URL, socket_timeout=2, socket_connect_timeout=2
    )

    async def heartbeat():
        while True:
            try:
                await state.heartbeat_redis.set(state.heartbeat_key, "1", ex=30)
            except RedisError:
                observability.log_event("worker_heartbeat_failed")
            await asyncio.sleep(10)

    state.heartbeat_task = asyncio.create_task(heartbeat())


async def stop(state):
    state.heartbeat_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await state.heartbeat_task
    try:
        await state.heartbeat_redis.delete(state.heartbeat_key)
    except RedisError:
        observability.log_event("worker_heartbeat_cleanup_failed")
    finally:
        await state.heartbeat_redis.aclose()
    if getattr(state, "metrics_server", None):
        state.metrics_server.shutdown()
        state.metrics_server.server_close()
