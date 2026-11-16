"""General background tasks handled by Taskiq."""

from app.common.logging import get_logger
from app.common.taskiq import broker

logger = get_logger(__name__)


@broker.task(
    task_name="maintenance.taskiq_heartbeat",
    schedule=[{"cron": "*/5 * * * *"}],
)
async def taskiq_heartbeat() -> None:
    """Emit a periodic heartbeat log so scheduler and worker liveness is visible."""
    logger.info("taskiq.heartbeat")
