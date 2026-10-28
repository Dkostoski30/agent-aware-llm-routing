"""JSON log lines (P1.3a-2, D12): one object per line, run fields at the top level."""

import json
import logging
import sys

from agent_runtime.logs import JsonFormatter


def record(message: str, **extra: object) -> logging.LogRecord:
    return logging.getLogger("agent_runtime.loop").makeRecord(
        "agent_runtime.loop", logging.WARNING, __file__, 1, message, (), None, extra=extra
    )


def test_a_run_line_is_one_json_object_with_its_fields() -> None:
    line = JsonFormatter().format(record("invalid call", **{"run-id": "run-1", "step": 2}))
    assert "\n" not in line
    fields = json.loads(line)
    assert fields["message"] == "invalid call"
    assert (fields["level"], fields["logger"]) == ("WARNING", "agent_runtime.loop")
    assert (fields["run-id"], fields["step"]) == ("run-1", 2)
    assert isinstance(fields["ts"], str)


def test_standard_record_attributes_stay_out() -> None:
    fields = json.loads(JsonFormatter().format(record("run started", **{"run-id": "run-1"})))
    assert set(fields) == {"ts", "level", "logger", "message", "run-id"}


def test_an_exception_goes_into_exc() -> None:
    try:
        raise ValueError("boom")
    except ValueError:
        line = record("crash")
        line.exc_info = sys.exc_info()
    fields = json.loads(JsonFormatter().format(line))
    assert "ValueError: boom" in fields["exc"]


def test_uvicorn_color_message_stays_out() -> None:
    line = record("Started server process", color_message="Started \x1b[36m%d\x1b[0m")
    assert "color_message" not in json.loads(JsonFormatter().format(line))
