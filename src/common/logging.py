#src/common/logging.py
import logging.config
import logging.handlers
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.common.config import Settings

# Create a logger for this module (before full setup)
logger = logging.getLogger("src")

# Setup paths
PROJECT_ROOT = Path(__file__).parent.parent.parent
LOG_CONFIG_PATH = PROJECT_ROOT / "logging.ini"

# Module-level variables (set by setup_logging)
LOG_DIR: Path = PROJECT_ROOT / "logs"


def setup_logging(settings: "Settings") -> Path:
    """
    Configure logging with file handlers based on settings from config.py.
    
    Args:
        settings: Settings instance from config.py (already loaded from .env)
    
    Returns:
        Path to the log directory
    """
    global LOG_DIR
    
    # Get log directory from settings
    log_dir_str = settings.LOG_DIR
    LOG_DIR = Path(log_dir_str) if Path(log_dir_str).is_absolute() else PROJECT_ROOT / log_dir_str
    
    # Ensure logs directory exists
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    
    # Load base config from .ini (formatters, console handler)
    if LOG_CONFIG_PATH.exists():
        logging.config.fileConfig(
            LOG_CONFIG_PATH,
            disable_existing_loggers=False,
        )
    else:
        logging.basicConfig(level=logging.INFO)
    
    # Get the detailed formatter from config
    detailed_formatter = logging.Formatter(
        fmt="%(asctime)s - %(name)s - %(levelname)s - %(filename)s:%(lineno)d - %(funcName)s - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )
    
    # Add rotating file handler for all logs
    file_handler = logging.handlers.RotatingFileHandler(
        filename=LOG_DIR / "app.log",
        maxBytes=settings.LOG_MAX_BYTES,
        backupCount=settings.LOG_BACKUP_COUNT,
        encoding="utf-8",
    )
    file_handler.setLevel(logging.INFO)
    file_handler.setFormatter(detailed_formatter)
    
    # Add rotating file handler for errors only
    error_handler = logging.handlers.RotatingFileHandler(
        filename=LOG_DIR / "error.log",
        maxBytes=settings.LOG_MAX_BYTES,
        backupCount=settings.LOG_BACKUP_COUNT,
        encoding="utf-8",
    )
    error_handler.setLevel(logging.ERROR)
    error_handler.setFormatter(detailed_formatter)
    
    # Add handlers to root logger
    root_logger = logging.getLogger()
    root_logger.addHandler(file_handler)
    root_logger.addHandler(error_handler)
    
    # Also add to app logger
    app_logger = logging.getLogger("src")
    app_logger.addHandler(file_handler)
    app_logger.addHandler(error_handler)
    
    # Setup access logger (separate from app logs)
    if settings.ACCESS_LOG_ENABLED:
        access_logger = logging.getLogger("access")
        access_logger.setLevel(logging.INFO)
        access_logger.propagate = False  # Don't propagate to root logger
        
        access_formatter = logging.Formatter(
            fmt='%(asctime)s - %(message)s',
            datefmt="%Y-%m-%d %H:%M:%S"
        )
        
        access_handler = logging.handlers.RotatingFileHandler(
            filename=LOG_DIR / "access.log",
            maxBytes=settings.LOG_MAX_BYTES,
            backupCount=settings.LOG_BACKUP_COUNT,
            encoding="utf-8",
        )
        access_handler.setLevel(logging.INFO)
        access_handler.setFormatter(access_formatter)
        access_logger.addHandler(access_handler)

    logger.info(f"Logging is set up. Log directory: {LOG_DIR}") 
    return LOG_DIR





def get_logger(name: str) -> logging.Logger:
    """
    Get a logger with the given name.
    
    Usage:
        from src.common.logging import get_logger
        logger = get_logger(__name__)
    """
    return logging.getLogger(name)


def get_access_logger() -> logging.Logger:
    """Get the access logger for HTTP request logging."""
    return logging.getLogger("access")