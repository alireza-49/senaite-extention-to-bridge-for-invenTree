"""Configuration loading and validation.

The configuration is a YAML file. Any string value may reference an
environment variable as ``${NAME}`` or ``${NAME:-default}`` so that secrets
never have to live in the YAML file itself.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Mapping, Sequence

import yaml


class ConfigError(ValueError):
    """Raised when the configuration is missing or invalid."""


_ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")

# Fields whose value must never be printed by ``describe()``/logs.
SECRET_FIELDS = frozenset(
    {"password", "token", "telegram_token", "smtp_password", "http_token"}
)


def _expand(value: Any, path: str) -> Any:
    """Recursively replace ``${VAR}`` references with environment values."""

    if isinstance(value, str):

        def replace(match: re.Match[str]) -> str:
            name, default = match.group(1), match.group(2)
            resolved = os.environ.get(name)
            if resolved:
                return resolved
            if default is not None:
                return default
            raise ConfigError(f"{path}: environment variable {name} is not set")

        return _ENV_PATTERN.sub(replace, value)
    if isinstance(value, list):
        return [_expand(item, f"{path}[{index}]") for index, item in enumerate(value)]
    if isinstance(value, Mapping):
        return {key: _expand(item, f"{path}.{key}") for key, item in value.items()}
    return value


def _section(data: Mapping[str, Any], key: str, path: str, required: bool = True) -> Mapping[str, Any]:
    value = data.get(key)
    if value is None:
        if required:
            raise ConfigError(f"{path}.{key} is required")
        return {}
    if not isinstance(value, Mapping):
        raise ConfigError(f"{path}.{key} must be a mapping")
    return value


def _string(section: Mapping[str, Any], key: str, path: str, default: str | None = None) -> str:
    value = section.get(key, default)
    if value is None:
        raise ConfigError(f"{path}.{key} is required")
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{path}.{key} must be a non-empty string")
    return value.strip()


def _boolean(section: Mapping[str, Any], key: str, path: str, default: bool) -> bool:
    value = section.get(key, default)
    if isinstance(value, bool):
        return value
    raise ConfigError(f"{path}.{key} must be true or false")


def _integer(section: Mapping[str, Any], key: str, path: str, default: int, minimum: int = 0) -> int:
    value = section.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"{path}.{key} must be an integer")
    if value < minimum:
        raise ConfigError(f"{path}.{key} must be >= {minimum}")
    return value


def _number(section: Mapping[str, Any], key: str, path: str, default: float) -> float:
    value = section.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"{path}.{key} must be a number")
    if value < 0:
        raise ConfigError(f"{path}.{key} must be >= 0")
    return float(value)


def _string_list(section: Mapping[str, Any], key: str, path: str) -> tuple[str, ...]:
    value = section.get(key, [])
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ConfigError(f"{path}.{key} must be a list of strings")
    return tuple(item.strip() for item in value if item.strip())


def _decimal(value: Any, path: str) -> Decimal:
    try:
        quantity = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ConfigError(f"{path} must be a number") from None
    if quantity <= 0:
        raise ConfigError(f"{path} must be greater than zero")
    return quantity


@dataclass(frozen=True)
class SenaiteConfig:
    base_url: str
    username: str
    password: str
    verify_ssl: bool = True
    timeout: float = 30.0
    trigger_states: tuple[str, ...] = ("verified", "published")
    poll_seconds: int = 60
    batch_size: int = 200
    rescan_days: float = 7.0
    initial_lookback_days: float = 0.0


@dataclass(frozen=True)
class InventreeConfig:
    base_url: str
    token: str
    verify_ssl: bool = True
    timeout: float = 30.0


@dataclass(frozen=True)
class ConsumeLine:
    part_ipn: str | None
    quantity: Decimal
    location: str | None = None
    notes: str | None = None
    part_pk: int | None = None

    def describe(self) -> str:
        target = f"part #{self.part_pk}" if self.part_pk else f"IPN {self.part_ipn}"
        suffix = f" @ {self.location}" if self.location else ""
        return f"{self.quantity} x {target}{suffix}"


@dataclass(frozen=True)
class Rule:
    id: str
    keywords: tuple[str, ...]
    sample_types: tuple[str, ...]
    consume: tuple[ConsumeLine, ...]

    def describe(self) -> str:
        keywords = ", ".join(self.keywords) or "<any>"
        sample_types = ", ".join(self.sample_types) or "<any>"
        parts = "; ".join(line.describe() for line in self.consume)
        return f"{self.id}: keywords=[{keywords}] sample_types=[{sample_types}] -> {parts}"


@dataclass(frozen=True)
class LowStockConfig:
    enabled: bool = True
    poll_seconds: int = 300
    repeat_after_hours: float = 24.0


@dataclass(frozen=True)
class NotifyConfig:
    telegram_token: str | None = None
    telegram_chat_ids: tuple[str, ...] = ()
    smtp_host: str | None = None
    smtp_port: int = 587
    smtp_user: str | None = None
    smtp_password: str | None = None
    smtp_from: str | None = None
    smtp_tls: bool = True
    email_to: tuple[str, ...] = ()
    webhook_url: str | None = None

    @property
    def channels(self) -> tuple[str, ...]:
        found = []
        if self.telegram_token and self.telegram_chat_ids:
            found.append("telegram")
        if self.smtp_host and self.email_to:
            found.append("email")
        if self.webhook_url:
            found.append("webhook")
        return tuple(found)


@dataclass(frozen=True)
class RunConfig:
    dry_run: bool = False
    journal_path: str = "/data/bridge.sqlite3"
    http_host: str = "0.0.0.0"
    http_port: int = 8099
    http_token: str | None = None


@dataclass(frozen=True)
class Config:
    senaite: SenaiteConfig
    inventree: InventreeConfig
    rules: tuple[Rule, ...] = ()
    low_stock: LowStockConfig = field(default_factory=LowStockConfig)
    notify: NotifyConfig = field(default_factory=NotifyConfig)
    run: RunConfig = field(default_factory=RunConfig)
    source: Path | None = None

    def describe(self) -> dict[str, Any]:
        """Return a log-safe summary (secrets replaced by ``***``)."""

        def redact(value: Any) -> Any:
            if isinstance(value, Mapping):
                return {key: ("***" if key in SECRET_FIELDS and item else redact(item)) for key, item in value.items()}
            if isinstance(value, (list, tuple)):
                return [redact(item) for item in value]
            if isinstance(value, Decimal):
                return str(value)
            return value

        return {
            "source": str(self.source) if self.source else None,
            "senaite": redact(
                {
                    "base_url": self.senaite.base_url,
                    "username": self.senaite.username,
                    "password": self.senaite.password,
                    "trigger_states": list(self.senaite.trigger_states),
                    "poll_seconds": self.senaite.poll_seconds,
                    "verify_ssl": self.senaite.verify_ssl,
                }
            ),
            "inventree": redact({"base_url": self.inventree.base_url, "token": self.inventree.token}),
            "rules": [rule.describe() for rule in self.rules],
            "low_stock": {
                "enabled": self.low_stock.enabled,
                "poll_seconds": self.low_stock.poll_seconds,
                "repeat_after_hours": self.low_stock.repeat_after_hours,
            },
            "notify": {
                "channels": list(self.notify.channels),
                "telegram_chat_ids": list(self.notify.telegram_chat_ids),
                "email_to": list(self.notify.email_to),
            },
            "run": {
                "dry_run": self.run.dry_run,
                "journal_path": self.run.journal_path,
                "http_port": self.run.http_port,
            },
        }


def _load_senaite(data: Mapping[str, Any]) -> SenaiteConfig:
    section = _section(data, "senaite", "config")
    states = _string_list(section, "trigger_states", "senaite") or ("verified", "published")
    return SenaiteConfig(
        base_url=_string(section, "base_url", "senaite").rstrip("/"),
        username=_string(section, "username", "senaite"),
        password=_string(section, "password", "senaite"),
        verify_ssl=_boolean(section, "verify_ssl", "senaite", True),
        timeout=_number(section, "timeout", "senaite", 30.0),
        trigger_states=states,
        poll_seconds=_integer(section, "poll_seconds", "senaite", 60, minimum=5),
        batch_size=_integer(section, "batch_size", "senaite", 200, minimum=1),
        rescan_days=_number(section, "rescan_days", "senaite", 7.0),
        initial_lookback_days=_number(section, "initial_lookback_days", "senaite", 0.0),
    )


def _load_inventree(data: Mapping[str, Any]) -> InventreeConfig:
    section = _section(data, "inventree", "config")
    return InventreeConfig(
        base_url=_string(section, "base_url", "inventree").rstrip("/"),
        token=_string(section, "token", "inventree"),
        verify_ssl=_boolean(section, "verify_ssl", "inventree", True),
        timeout=_number(section, "timeout", "inventree", 30.0),
    )


def _load_rule(raw: Any, index: int) -> Rule:
    path = f"rules[{index}]"
    if not isinstance(raw, Mapping):
        raise ConfigError(f"{path} must be a mapping")
    rule_id = _string(raw, "id", path)
    match = _section(raw, "senaite", path, required=False)
    keywords = _string_list(match, "keywords", f"{path}.senaite")
    sample_types = _string_list(match, "sample_types", f"{path}.senaite")
    consume_raw = raw.get("consume")
    if not isinstance(consume_raw, list) or not consume_raw:
        raise ConfigError(f"{path}.consume must be a non-empty list")
    consume: list[ConsumeLine] = []
    for line_index, line in enumerate(consume_raw):
        line_path = f"{path}.consume[{line_index}]"
        if not isinstance(line, Mapping):
            raise ConfigError(f"{line_path} must be a mapping")
        part_ipn = line.get("part_ipn")
        part_pk = line.get("part_pk")
        if not part_ipn and part_pk is None:
            raise ConfigError(f"{line_path} needs part_ipn or part_pk")
        if part_pk is not None and (isinstance(part_pk, bool) or not isinstance(part_pk, int)):
            raise ConfigError(f"{line_path}.part_pk must be an integer")
        if "quantity" not in line:
            raise ConfigError(f"{line_path}.quantity is required")
        consume.append(
            ConsumeLine(
                part_ipn=str(part_ipn).strip() if part_ipn else None,
                part_pk=part_pk,
                quantity=_decimal(line["quantity"], f"{line_path}.quantity"),
                location=str(line["location"]).strip() if line.get("location") else None,
                notes=str(line["notes"]).strip() if line.get("notes") else None,
            )
        )
    if not keywords and not sample_types:
        raise ConfigError(f"{path}.senaite needs at least one keyword or sample type")
    return Rule(id=rule_id, keywords=keywords, sample_types=sample_types, consume=tuple(consume))


def _load_notify(data: Mapping[str, Any]) -> NotifyConfig:
    section = _section(data, "notify", "config", required=False)
    telegram = _section(section, "telegram", "notify", required=False)
    email = _section(section, "email", "notify", required=False)
    token = telegram.get("token") or os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_ids = list(_string_list(telegram, "chat_ids", "notify.telegram"))
    single_chat = telegram.get("chat_id")
    if single_chat:
        chat_ids.append(str(single_chat))
    smtp_host = email.get("host")
    return NotifyConfig(
        telegram_token=str(token) if token else None,
        telegram_chat_ids=tuple(dict.fromkeys(chat_ids)),
        smtp_host=str(smtp_host) if smtp_host else None,
        smtp_port=_integer(email, "port", "notify.email", 587, minimum=1),
        smtp_user=str(email["user"]) if email.get("user") else None,
        smtp_password=str(email["password"]) if email.get("password") else None,
        smtp_from=str(email.get("from") or email.get("user") or "") or None,
        smtp_tls=_boolean(email, "tls", "notify.email", True),
        email_to=_string_list(email, "to", "notify.email"),
        webhook_url=str(section["webhook_url"]).strip() if section.get("webhook_url") else None,
    )


def _load_run(data: Mapping[str, Any]) -> RunConfig:
    section = _section(data, "run", "config", required=False)
    http_token = section.get("http_token") or os.environ.get("BRIDGE_TOKEN")
    return RunConfig(
        dry_run=_boolean(section, "dry_run", "run", False),
        journal_path=_string(section, "journal_path", "run", "/data/bridge.sqlite3"),
        http_host=_string(section, "http_host", "run", "0.0.0.0"),
        http_port=_integer(section, "http_port", "run", 8099, minimum=1),
        http_token=str(http_token) if http_token else None,
    )


def load_config(path: str | os.PathLike[str]) -> Config:
    """Read ``path``, expand environment variables and validate the result."""

    source = Path(path)
    if not source.is_file():
        raise ConfigError(f"configuration file not found: {source}")
    try:
        raw = yaml.safe_load(source.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:  # pragma: no cover - message passthrough
        raise ConfigError(f"invalid YAML in {source}: {exc}") from None
    if not isinstance(raw, Mapping):
        raise ConfigError(f"{source} must contain a YAML mapping")
    data = _expand(raw, "config")

    rules_raw = data.get("rules") or []
    if not isinstance(rules_raw, list):
        raise ConfigError("rules must be a list")
    rules = tuple(_load_rule(rule, index) for index, rule in enumerate(rules_raw))
    seen: set[str] = set()
    for rule in rules:
        if rule.id in seen:
            raise ConfigError(f"duplicate rule id: {rule.id}")
        seen.add(rule.id)

    low_raw = _section(data, "low_stock", "config", required=False)
    low_stock = LowStockConfig(
        enabled=_boolean(low_raw, "enabled", "low_stock", True),
        poll_seconds=_integer(low_raw, "poll_seconds", "low_stock", 300, minimum=10),
        repeat_after_hours=_number(low_raw, "repeat_after_hours", "low_stock", 24.0),
    )

    return Config(
        senaite=_load_senaite(data),
        inventree=_load_inventree(data),
        rules=rules,
        low_stock=low_stock,
        notify=_load_notify(data),
        run=_load_run(data),
        source=source,
    )


def validate_for_run(config: Config) -> list[str]:
    """Return configuration problems that would stop a real (non dry) run."""

    problems: list[str] = []
    if not config.rules:
        problems.append("no rules configured: nothing would ever be consumed")
    if config.run.dry_run:
        return problems
    if not config.notify.channels:
        problems.append("no notification channel configured (telegram/email/webhook)")
    if not config.run.http_token:
        problems.append("run.http_token / BRIDGE_TOKEN is empty: the HTTP job endpoint stays disabled")
    return problems
