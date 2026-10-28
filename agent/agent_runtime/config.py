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
    # Limits on the files of a run request (D13) and on the rows of a tool result (P1.6a).
    max_file_bytes: int
    max_files: int
    max_result_rows: int
    # Decimals the CSV tools round their numbers to (P1.6c).
    result_decimals: int


def load_config(path: Path) -> AgentConfig:
    """Reads model.name and the agent section of a profile; a missing key raises KeyError."""
    profile = yaml.safe_load(path.read_text())
    agent = profile["agent"]
    if agent["tool_choice"] not in get_args(ToolChoice):
        raise ValueError(
            f"agent.tool_choice is {agent['tool_choice']!r}, expected one of {get_args(ToolChoice)}"
        )
    return AgentConfig(
        model_name=profile["model"]["name"],
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
        result_decimals=agent["result_decimals"],
    )
