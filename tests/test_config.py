from decimal import Decimal
from pathlib import Path

import pytest
import yaml

from bridge.config import ConfigError, load_config, validate_for_run

ROOT = Path(__file__).resolve().parent.parent

BASE = {
    "senaite": {"base_url": "http://localhost:8080/SENAITE", "username": "api", "password": "pw"},
    "inventree": {"base_url": "https://inv.example.org", "token": "tok"},
    "rules": [
        {
            "id": "tpc",
            "senaite": {"keywords": ["TPC"], "sample_types": ["water"]},
            "consume": [{"part_ipn": "PETRI-90", "quantity": 2}],
        }
    ],
}


def write(tmp_path: Path, data: dict) -> Path:
    path = tmp_path / "config.yml"
    path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    return path


def test_loads_and_expands_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("INVENTREE_TOKEN", "from-env")
    data = yaml.safe_load(yaml.safe_dump(BASE))
    data["inventree"]["token"] = "${INVENTREE_TOKEN}"
    data["run"] = {"http_token": "${BRIDGE_TOKEN:-fallback-token}"}

    config = load_config(write(tmp_path, data))

    assert config.inventree.token == "from-env"
    assert config.run.http_token == "fallback-token"
    assert config.rules[0].consume[0].quantity == Decimal("2")
    assert config.senaite.trigger_states == ("verified", "published")


def test_missing_environment_variable_is_an_error(tmp_path, monkeypatch):
    monkeypatch.delenv("NOT_SET_ANYWHERE", raising=False)
    data = yaml.safe_load(yaml.safe_dump(BASE))
    data["inventree"]["token"] = "${NOT_SET_ANYWHERE}"
    with pytest.raises(ConfigError, match="NOT_SET_ANYWHERE"):
        load_config(write(tmp_path, data))


def test_duplicate_rule_ids_rejected(tmp_path):
    data = yaml.safe_load(yaml.safe_dump(BASE))
    data["rules"].append(dict(data["rules"][0]))
    with pytest.raises(ConfigError, match="duplicate rule id"):
        load_config(write(tmp_path, data))


def test_rule_needs_a_matcher_and_positive_quantity(tmp_path):
    data = yaml.safe_load(yaml.safe_dump(BASE))
    data["rules"][0]["senaite"] = {}
    with pytest.raises(ConfigError, match="keyword or sample type"):
        load_config(write(tmp_path, data))

    data = yaml.safe_load(yaml.safe_dump(BASE))
    data["rules"][0]["consume"][0]["quantity"] = 0
    with pytest.raises(ConfigError, match="greater than zero"):
        load_config(write(tmp_path, data))


def test_describe_never_leaks_secrets(tmp_path):
    data = yaml.safe_load(yaml.safe_dump(BASE))
    data["notify"] = {"telegram": {"token": "super-secret-token", "chat_ids": ["42"]}}
    config = load_config(write(tmp_path, data))

    described = config.describe()

    assert "super-secret-token" not in str(described)
    assert described["notify"]["channels"] == ["telegram"]
    assert config.notify.telegram_chat_ids == ("42",)


def test_example_config_is_valid(monkeypatch):
    monkeypatch.setenv("SENAITE_USER", "api")
    monkeypatch.setenv("SENAITE_PASSWORD", "pw")
    monkeypatch.setenv("INVENTREE_TOKEN", "tok")
    config = load_config(ROOT / "config.example.yml")
    assert [rule.id for rule in config.rules] == ["microbiology-tpc", "microbiology-coliform"]


def test_validate_warns_when_nothing_would_happen(tmp_path):
    data = yaml.safe_load(yaml.safe_dump(BASE))
    data["rules"] = []
    config = load_config(write(tmp_path, data))
    problems = validate_for_run(config)
    assert any("no rules" in problem for problem in problems)
