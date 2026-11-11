"""The agent's settings, read from a config/ profile (D11)."""

from dataclasses import dataclass
from pathlib import Path
from typing import Literal, cast, get_args

import yaml

# D10: "required" lets vLLM constrain every call to the tool schemas, "auto" lets the runtime check
# each call.
ToolChoice = Literal["required", "auto"]


@dataclass(frozen=True)
class Sampling:
    temperature: float
    top_p: float
    top_k: int


@dataclass(frozen=True)
class AgentConfig:
    model_name: str
    tool_choice: ToolChoice
    max_steps: int
    max_invalid_calls: int
    expected_steps_default: int
    llm_timeout_seconds: float
    history_ttl_seconds: int
    sampling: Sampling
    system_prompt: str
    # Limits on the files of a run request (D13) and on the rows of a tool result (P1.6a), and on
    # the group names a CSV tool lists (P2.6c).
    max_file_bytes: int
    max_files: int
    max_result_rows: int
    max_result_chars: int
    # Decimals the CSV tools round their numbers to (P1.6c).
    result_decimals: int
    # Bounds on what the contracts tools put into the context (P2.1a-1).
    max_clause_chars: int
    max_diff_changes: int
    diff_context_words: int
    # How often GET /runs/{id}/events reads the run from Redis for new steps (P1.4b-1).
    events_poll_seconds: float
    # Whether a run's end is sent to the picker, POST /runs/{id}/end (P3.3, D1): routing.plugin,
    # since only the plugin serves it.
    end_of_run: bool


def load_config(path: Path) -> AgentConfig:
    """Reads the model's name, the agent section and the routing switch of a profile; a missing key
    raises KeyError.

    With model.lora set, the name is the adapter's: vLLM serves the adapter under its own name next
    to the base model, and only a request that names it gets it (P1.7)."""
    profile = yaml.safe_load(path.read_text())
    model = profile["model"]
    agent = profile["agent"]
    if agent["tool_choice"] not in get_args(ToolChoice):
        raise ValueError(
            f"agent.tool_choice is {agent['tool_choice']!r}, expected one of {get_args(ToolChoice)}"
        )
    plugin = profile["routing"]["plugin"]
    if not isinstance(plugin, bool):
        raise ValueError(f"routing.plugin is {plugin!r}, expected true or false")
    return AgentConfig(
        model_name=model["lora"]["name"] if "lora" in model else model["name"],
        tool_choice=cast(ToolChoice, agent["tool_choice"]),
        max_steps=agent["max_steps"],
        max_invalid_calls=agent["max_invalid_calls"],
        expected_steps_default=agent["expected_steps_default"],
        llm_timeout_seconds=agent["llm_timeout_seconds"],
        history_ttl_seconds=agent["history_ttl_seconds"],
        sampling=Sampling(**agent["sampling"]),
        system_prompt=agent["system_prompt"],
        max_file_bytes=agent["max_file_bytes"],
        max_files=agent["max_files"],
        max_result_rows=agent["max_result_rows"],
        max_result_chars=agent["max_result_chars"],
        result_decimals=agent["result_decimals"],
        max_clause_chars=agent["max_clause_chars"],
        max_diff_changes=agent["max_diff_changes"],
        diff_context_words=agent["diff_context_words"],
        events_poll_seconds=agent["events_poll_seconds"],
        end_of_run=plugin,
    )
