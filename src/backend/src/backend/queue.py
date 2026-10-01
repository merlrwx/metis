import os

from taskiq import SimpleRetryMiddleware
from taskiq_redis import RedisStreamBroker

REDIS_URL = os.environ.get("REDIS_URL")
QUEUE_CONFIGURED = bool(REDIS_URL)

broker = RedisStreamBroker(
    REDIS_URL or "redis://127.0.0.1:6379/0",
    queue_name="metis:jobs",
    consumer_group_name="metis-workers",
    xread_block=int(os.environ.get("TASKIQ_XREAD_BLOCK_MS", "1000")),
    idle_timeout=int(os.environ.get("TASKIQ_IDLE_TIMEOUT_MS", "60000")),
    unacknowledged_lock_timeout=15,
).with_middlewares(SimpleRetryMiddleware(default_retry_count=2))
