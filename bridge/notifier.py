"""Notification delivery: Telegram Bot API, SMTP e-mail and an optional webhook.

Every send attempt returns a result entry instead of raising, so one broken
channel never stops the bridge (and never stops the stock deduction itself).
"""

from __future__ import annotations

import logging
import smtplib
from email.message import EmailMessage
from typing import Any, Mapping, Sequence

import requests

from .config import NotifyConfig

LOGGER = logging.getLogger(__name__)

TELEGRAM_TIMEOUT = 15


class Notifier:
    def __init__(
        self,
        config: NotifyConfig,
        dry_run: bool = False,
        session: requests.Session | None = None,
    ) -> None:
        self.config = config
        self.dry_run = dry_run
        self._http = session or requests.Session()

    @property
    def channels(self) -> tuple[str, ...]:
        return self.config.channels

    def send(self, subject: str, text: str, payload: Mapping[str, Any] | None = None) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        if not self.channels:
            LOGGER.warning("no notification channel configured; message not delivered: %s", subject)
            return results
        for channel in self.channels:
            handler = getattr(self, f"_send_{channel}")
            try:
                results.append(handler(subject, text, payload))
            except Exception as exc:  # noqa: BLE001 - a channel must not break the run
                LOGGER.error("notification via %s failed: %s", channel, exc)
                results.append({"channel": channel, "status": "error", "error": str(exc)})
        return results

    # -- channels ----------------------------------------------------------

    def _send_telegram(self, subject: str, text: str, payload: Mapping[str, Any] | None) -> dict[str, Any]:
        if self.dry_run:
            return {"channel": "telegram", "status": "dry_run"}
        token = self.config.telegram_token or ""
        message = f"{subject}\n\n{text}"
        for chat_id in self.config.telegram_chat_ids:
            response = self._http.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": chat_id, "text": message[:4000], "disable_web_page_preview": True},
                timeout=TELEGRAM_TIMEOUT,
            )
            if response.status_code >= 400:
                raise RuntimeError(f"Telegram HTTP {response.status_code}: {response.text[:200].strip()}")
        return {"channel": "telegram", "status": "sent", "recipients": len(self.config.telegram_chat_ids)}

    def _send_email(self, subject: str, text: str, payload: Mapping[str, Any] | None) -> dict[str, Any]:
        if self.dry_run:
            return {"channel": "email", "status": "dry_run"}
        message = EmailMessage()
        message["Subject"] = subject
        message["From"] = self.config.smtp_from or self.config.smtp_user or "senaite-bridge@localhost"
        message["To"] = ", ".join(self.config.email_to)
        message.set_content(text)
        host = self.config.smtp_host or ""
        with smtplib.SMTP(host, self.config.smtp_port, timeout=30) as smtp:
            if self.config.smtp_tls:
                smtp.starttls()
            if self.config.smtp_user:
                smtp.login(self.config.smtp_user, self.config.smtp_password or "")
            smtp.send_message(message)
        return {"channel": "email", "status": "sent", "recipients": len(self.config.email_to)}

    def _send_webhook(self, subject: str, text: str, payload: Mapping[str, Any] | None) -> dict[str, Any]:
        if self.dry_run:
            return {"channel": "webhook", "status": "dry_run"}
        response = self._http.post(
            self.config.webhook_url or "",
            json={"subject": subject, "text": text, "payload": dict(payload or {})},
            timeout=20,
        )
        if response.status_code >= 400:
            raise RuntimeError(f"webhook HTTP {response.status_code}: {response.text[:200].strip()}")
        return {"channel": "webhook", "status": "sent"}


def consumption_message(
    *,
    sample_id: str,
    analysis_uid: str,
    keyword: str,
    result: str,
    unit: str,
    rule_id: str,
    parts: Sequence[str],
) -> tuple[str, str]:
    subject = f"SENAITE: موجودی برای آزمون {sample_id} کسر شد"
    text = "\n".join(
        [
            f"نمونه: {sample_id}",
            f"آزمون: {keyword} → {result} {unit}".rstrip(),
            f"قاعده: {rule_id}",
            "کالاهای کسرشده:",
            *[f"  - {part}" for part in parts],
            f"UID آزمون: {analysis_uid}",
        ]
    )
    return subject, text


def insufficient_message(
    *,
    sample_id: str,
    keyword: str,
    rule_id: str,
    shortages: Sequence[str],
) -> tuple[str, str]:
    subject = f"SENAITE: موجودی ناکافی برای آزمون {sample_id}"
    text = "\n".join(
        [
            f"نمونه: {sample_id}",
            f"آزمون: {keyword}",
            f"قاعده: {rule_id}",
            "موجودی کافی نیست؛ هیچ کسری انجام نشد:",
            *[f"  - {item}" for item in shortages],
            "پس از تأمین موجودی، در چرخهٔ بعدی دوباره بررسی می‌شود.",
        ]
    )
    return subject, text


def low_stock_message(parts: Sequence[Mapping[str, Any]]) -> tuple[str, str]:
    subject = f"InvenTree: {len(parts)} کالا زیر حد موجودی"
    lines = ["کالاهای زیر به حد موجودی رسیده‌اند:", ""]
    for part in parts:
        label = part.get("IPN") or part.get("name") or part.get("pk")
        lines.append(
            f"- {label} ({part.get('name', '')}): موجودی {part.get('in_stock')} از حد {part.get('minimum_stock')}"
        )
    return subject, "\n".join(lines)


def partial_message(*, sample_id: str, keyword: str, rule_id: str, error: str, removed: Sequence[Any]) -> tuple[str, str]:
    subject = f"SENAITE: کسر نیمه‌کاره برای {sample_id} — بررسی دستی لازم است"
    text = "\n".join(
        [
            f"نمونه: {sample_id}",
            f"آزمون: {keyword}",
            f"قاعده: {rule_id}",
            f"خطا: {error}",
            "بخشی از کالاها کسر شده است:",
            *[f"  - stock item {item.get('pk')}: {item.get('quantity')}" for item in removed],
            "برای جلوگیری از کسری دوباره، این مورد به‌صورت خودکار تکرار نمی‌شود.",
            "لطفاً موجودی را دستی بررسی و ژورنال را اصلاح کنید.",
        ]
    )
    return subject, text


def failure_message(*, sample_id: str, keyword: str, rule_id: str, error: str) -> tuple[str, str]:
    subject = f"SENAITE: خطا در کسر موجودی برای {sample_id}"
    text = "\n".join(
        [
            f"نمونه: {sample_id}",
            f"آزمون: {keyword}",
            f"قاعده: {rule_id}",
            f"خطا: {error}",
            "این مورد در چرخهٔ بعدی دوباره تلاش می‌شود.",
        ]
    )
    return subject, text
