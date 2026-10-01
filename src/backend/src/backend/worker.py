import asyncio
import os
import sys
from pathlib import Path

from backend.queue import QUEUE_CONFIGURED, broker
from backend.tasks import wake_worker


async def publish_startup_wakeup() -> None:
    await broker.startup()
    try:
        # RedisStreamBroker checks abandoned entries after reading a new event.
        await wake_worker.kiq()
    finally:
        await broker.shutdown()


def main() -> None:
    if not QUEUE_CONFIGURED:
        raise SystemExit("REDIS_URL must be configured to start the worker")
    asyncio.run(publish_startup_wakeup())
    taskiq = Path(sys.executable).with_name("taskiq")
    os.execv(
        taskiq,
        [
            str(taskiq),
            "worker",
            "--workers",
            "1",
            "--max-async-tasks",
            "1",
            "--ack-type",
            "when_executed",
            "backend.queue:broker",
            "backend.tasks",
        ],
    )
