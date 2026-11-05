import logging
import os


def configure_logging():
    """Send this package's logs to stderr (uvicorn only configures its own)."""
    logger = logging.getLogger("ethiobank_receipts")
    if logger.handlers:
        return
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    logger.addHandler(handler)
    logger.setLevel(os.environ.get("ETHIOBANK_LOG_LEVEL", "INFO").upper())
    logger.propagate = False
