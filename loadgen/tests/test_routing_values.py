"""infra/routing-values.sh and infra/set-routing.sh (P3.2): which Helm values the picker gets for a
profile's routing section and a variant's rules, and what is refused before helm runs.

Both scripts run from a copy of infra/ in a temporary repo, with sample profiles, and set-routing.sh
with fake helm, kubectl and curl that record their arguments. No cluster: make up and set-routing on
the cluster are what card 37's done-when proves. Both bugs found there were in lines these reach.
"""

import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
INFRA_FILES = (
    "routing-values.sh",
    "set-routing.sh",
    "profile.sh",
    "router-chart.sh",
    "picker-image.sh",
)
RULES_OFF = {
    "plugin": False,
    "affinity": False,
    "overload_guard": False,
    "parallel_spill": False,
    "priority": False,
    "warmup": False,
}


def repo(tmp_path: Path, routing: Any = None) -> Path:
    """A repo with infra/'s scripts, an empty gateway/ for picker-image.sh's hash, and profile p."""
    (tmp_path / "infra" / "helm").mkdir(parents=True)
    for name in INFRA_FILES:
        shutil.copy(REPO_ROOT / "infra" / name, tmp_path / "infra" / name)
    (tmp_path / "infra" / "helm" / "router-values.yaml").write_text("router: {}\n")
    (tmp_path / "gateway").mkdir()
    (tmp_path / "gateway" / "go.mod").write_text("module example\n")
    (tmp_path / "config").mkdir()
    profile = {"model": {"name": "m"}, "routing": RULES_OFF if routing is None else routing}
    (tmp_path / "config" / "p.yaml").write_text(yaml.safe_dump(profile))
    return tmp_path


def rules_file(directory: Path, content: Any) -> Path:
    path = directory / "rules.yaml"
    path.write_text(yaml.safe_dump(content))
    return path


def routing_values(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(root / "infra" / "routing-values.sh"), *args],
        capture_output=True,
        text=True,
        check=False,
    )


def picker_config(stdout: str) -> dict[str, Any]:
    epp = yaml.safe_load(stdout)["router"]["epp"]
    assert epp["pluginsConfigFile"] == "thesis-plugins.yaml"
    config: dict[str, Any] = yaml.safe_load(epp["pluginsCustomConfig"]["thesis-plugins.yaml"])
    return config


def test_plugin_off_leaves_the_charts_default(tmp_path: Path) -> None:
    done = routing_values(repo(tmp_path), "p")
    assert done.returncode == 0, done.stderr
    assert yaml.safe_load(done.stdout) == {}


def test_plugin_on_puts_the_run_router_before_the_defaults_scorers(tmp_path: Path) -> None:
    root = repo(tmp_path)
    rules = rules_file(tmp_path, {"routing": {"plugin": True, "overload_guard": True}})
    done = routing_values(root, "p", str(rules))
    assert done.returncode == 0, done.stderr
    config = picker_config(done.stdout)
    plugins = {plugin["type"]: plugin.get("parameters") for plugin in config["plugins"]}
    assert plugins["thesis-run-router"] == {
        "affinity": False,
        "overloadGuard": True,
        "parallelSpill": False,
        "priority": False,
        "warmup": False,
    }
    assert plugins["metrics-data-source"] == {
        "scheme": "http",
        "path": "/metrics",
        "insecureSkipVerify": True,
    }
    assert set(plugins) == {
        "thesis-run-router",
        "queue-scorer",
        "kv-cache-utilization-scorer",
        "prefix-cache-scorer",
        "metrics-data-source",
        "core-metrics-extractor",
    }
    (profile,) = config["schedulingProfiles"]
    assert profile["plugins"] == [
        {"pluginRef": "thesis-run-router"},
        {"pluginRef": "queue-scorer", "weight": 2},
        {"pluginRef": "kv-cache-utilization-scorer", "weight": 2},
        {"pluginRef": "prefix-cache-scorer", "weight": 3},
    ]


@pytest.mark.parametrize("profile", ["real", "real-plain", "sim", "demo"])
def test_every_profile_has_every_rule_and_the_plugin_off(profile: str) -> None:
    done = routing_values(REPO_ROOT, profile)
    assert done.returncode == 0, done.stderr
    assert yaml.safe_load(done.stdout) == {}


@pytest.mark.parametrize(
    ("routing", "rules", "says"),
    [
        # A typo in a variant's rules.
        (None, {"routing": {"plugin": True, "afinity": True}}, "afinity"),
        (None, {"routing": {"plugin": "yes"}}, "true or false"),
        # A rule on with the plugin off would do nothing, without a word.
        (None, {"routing": {"affinity": True}}, "plugin is off"),
        ({**RULES_OFF, "affinity": True}, None, "plugin is off"),
        # A profile without a rule would hand the plugin null, which it refuses only at start.
        ({key: on for key, on in RULES_OFF.items() if key != "warmup"}, None, "routing.warmup"),
        ({key: on for key, on in RULES_OFF.items() if key != "plugin"}, None, "routing.plugin"),
        # A typo in the profile would let the same typo in a variant through.
        ({**RULES_OFF, "afinity": False}, None, "routing.afinity"),
        (True, None, "map"),
        (None, {"routing": True}, "map"),
        (None, {"routing": ["plugin"]}, "map"),
        (None, {"routing": None}, "map"),
        (None, {"other": {}}, "map"),
    ],
)
def test_rules_it_cannot_trust_are_refused(
    tmp_path: Path, routing: Any, rules: Any, says: str
) -> None:
    root = repo(tmp_path, routing)
    args = ["p"] if rules is None else ["p", str(rules_file(tmp_path, rules))]
    done = routing_values(root, *args)
    assert done.returncode != 0
    assert done.stdout == ""
    assert says in done.stderr


def test_a_profile_that_is_not_there_is_refused(tmp_path: Path) -> None:
    done = routing_values(repo(tmp_path), "nope")
    assert done.returncode != 0
    assert "nope" in done.stderr


FAKE_HELM = """#!/usr/bin/env bash
# Records each --values file's content, in order, one YAML document each.
while [[ $# -gt 0 ]]; do
  if [[ $1 == --values ]]; then
    { echo '---'; cat "$2"; echo; } >>"$LOG"
    shift
  fi
  shift
done
"""
FAKE_KUBECTL = """#!/usr/bin/env bash
if [[ $* == *"get service"* ]]; then echo 10.0.0.1; fi
"""
FAKE_CURL = """#!/usr/bin/env bash
echo curl >>"$LOG.curl"
"""


def set_routing(root: Path, tmp_path: Path, *args: str) -> tuple[int, list[Any], str]:
    """Exit code, the --values documents helm got, and the output."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    for name, text in (("helm", FAKE_HELM), ("kubectl", FAKE_KUBECTL), ("curl", FAKE_CURL)):
        (bin_dir / name).write_text(text)
        (bin_dir / name).chmod(0o755)
    log = tmp_path / "helm.log"
    done = subprocess.run(
        ["bash", str(root / "infra" / "set-routing.sh"), "p", *args],
        capture_output=True,
        text=True,
        check=False,
        # The fakes first; yq, jq and the rest are the machine's.
        env={**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}", "LOG": str(log)},
    )
    documents = list(yaml.safe_load_all(log.read_text())) if log.exists() else []
    return done.returncode, documents, done.stdout + done.stderr


def test_set_routing_without_a_file_runs_the_profiles_routing(tmp_path: Path) -> None:
    root = repo(tmp_path)
    code, documents, output = set_routing(root, tmp_path)
    assert code == 0, output
    router, picker, routing = documents
    assert router == {"router": {}}
    assert picker["router"]["epp"]["image"]["tag"]
    assert routing == {}
    assert "set-routing: config/p.yaml is serving, on try 1" in output


def test_set_routing_lays_a_rules_file_over_the_profile(tmp_path: Path) -> None:
    root = repo(tmp_path)
    rules = rules_file(tmp_path, {"routing": {"plugin": True}})
    code, documents, output = set_routing(root, tmp_path, str(rules))
    assert code == 0, output
    _router, _picker, routing = documents
    assert routing["router"]["epp"]["pluginsConfigFile"] == "thesis-plugins.yaml"
    assert f"set-routing: {rules} is serving" in output


def test_set_routing_passes_a_values_file_to_helm_last(tmp_path: Path) -> None:
    root = repo(tmp_path)
    values = tmp_path / "variant-values.yaml"
    values.write_text("router: {epp: {pluginsConfigFile: round-robin.yaml}}\n")
    code, documents, output = set_routing(root, tmp_path, str(values))
    assert code == 0, output
    assert len(documents) == 4
    assert documents[2] == {}
    assert documents[3] == {"router": {"epp": {"pluginsConfigFile": "round-robin.yaml"}}}


@pytest.mark.parametrize(
    ("content", "says"),
    [
        ("routing: {plugin: true}\nrouter: {}\n", "routing and other keys"),
        ("routing: [\n", "bad file"),
    ],
)
def test_set_routing_refuses_a_file_before_helm_runs(
    tmp_path: Path, content: str, says: str
) -> None:
    root = repo(tmp_path)
    path = tmp_path / "bad.yaml"
    path.write_text(content)
    code, documents, output = set_routing(root, tmp_path, str(path))
    assert code != 0
    assert documents == []
    assert says in output
