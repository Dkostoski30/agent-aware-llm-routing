"""JSON log lines (D12): one object per line, the fields a line was logged with at the top level.

The app backend's lines are outside a run, so each carries the request-id of the request it
belongs to. The formatter is the agent's (agent_runtime/logs.py), copied: importing it would put the
agent package and its dependencies (redis, jsonschema) into the app's image.
"""

import json
import logging
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any

# Set by the app's middleware for the length of one request.
REQUEST_ID: ContextVar[str] = ContextVar("request_id")

# Attributes every LogRecord has; anything else on a record came from extra= (request-id, ...).
# uvicorn adds color_message, its message with ANSI colour codes; message already has the text.
_LEFT_OUT = set(vars(logging.makeLogRecord({}))) | {"message", "asctime", "color_message"}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        line: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        line |= {k: v for k, v in vars(record).items() if k not in _LEFT_OUT}
        if record.exc_info is not None:
            line["exc"] = self.formatException(record.exc_info)
        # default=str: a field that is not JSON (an exception object, a path) is still logged.
        return json.dumps(line, default=str)


def configure() -> None:
    """Every logger, uvicorn's included, writes JSON lines to stderr at INFO."""
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    logging.basicConfig(level=logging.INFO, handlers=[handler], force=True)


def log(logger: logging.Logger, level: int, message: str, **fields: Any) -> None:
    """A line of the current request; outside a request REQUEST_ID.get() raises LookupError."""
    logger.log(level, message, extra={"request-id": REQUEST_ID.get(), **fields})
