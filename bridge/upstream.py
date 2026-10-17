"""Upstream request construction, SSE usage extraction and cost accounting."""

from __future__ import annotations

import json
import logging
from decimal import Decimal

from django.conf import settings

from .models import Account, ModelPrice

logger = logging.getLogger("bridge")

OAUTH_BETA = "oauth-2025-04-20"


def upstream_url(account: Account, path: str) -> str:
    base = account.base_url.rstrip("/")
    if not path.startswith("/"):
        path = "/" + path
    return base + path


OPENAI_FORWARD_HEADERS = (
    "content-type",
    "accept",
    "user-agent",
    "openai-beta",
    "originator",
    "session_id",
    "conversation_id",
    "chatgpt-account-id",
)


def build_upstream_headers(account: Account, incoming) -> dict[str, str]:
    """Copy the client headers that matter and swap in the account credentials."""
    if account.protocol == Account.PROTOCOL_OPENAI:
        headers: dict[str, str] = {}
        for key in OPENAI_FORWARD_HEADERS:
            value = incoming.get(key)
            if value:
                headers[key] = value
        headers["authorization"] = f"Bearer {account.credential_plain}"
        for key, value in (account.extra_headers or {}).items():
            headers[str(key)] = str(value)
        return headers

    headers: dict[str, str] = {}
    content_type = incoming.get("content-type")
    if content_type:
        headers["content-type"] = content_type
    accept = incoming.get("accept")
    if accept:
        headers["accept"] = accept
    headers["anthropic-version"] = incoming.get("anthropic-version") or "2023-06-01"
    user_agent = incoming.get("user-agent")
    if user_agent:
        headers["user-agent"] = user_agent

    beta = incoming.get("anthropic-beta") or ""
    if account.auth_type == Account.AUTH_OAUTH:
        headers["authorization"] = f"Bearer {account.credential_plain}"
        if OAUTH_BETA not in beta:
            beta = ",".join(part for part in (beta, OAUTH_BETA) if part)
    else:
        headers["x-api-key"] = account.credential_plain

    if beta:
        headers["anthropic-beta"] = beta
    for key, value in (account.extra_headers or {}).items():
        headers[str(key)] = str(value)
    return headers


# ---------------------------------------------------------------------------
# Pricing
# ---------------------------------------------------------------------------
def price_for(model: str) -> ModelPrice | None:
    if not model:
        return None
    # ModelPrice is ordered by -model_prefix, so the longest prefix matches first.
    for price in ModelPrice.objects.all():
        if model.startswith(price.model_prefix):
            return price
    return None


def calculate_cost(model: str, usage: dict) -> Decimal | None:
    price = price_for(model)
    if price is None:
        return None
    million = Decimal("1000000")
    cost = (
        Decimal(int(usage.get("input_tokens") or 0)) * price.input_per_mtok
        + Decimal(int(usage.get("output_tokens") or 0)) * price.output_per_mtok
        + Decimal(int(usage.get("cache_read_input_tokens") or 0)) * price.cache_read_per_mtok
        + Decimal(int(usage.get("cache_creation_input_tokens") or 0)) * price.cache_write_per_mtok
    ) / million
    return cost.quantize(Decimal("0.000001"))


# ---------------------------------------------------------------------------
# Streaming usage
# ---------------------------------------------------------------------------
USAGE_KEYS = (
    "input_tokens",
    "output_tokens",
    "cache_read_input_tokens",
    "cache_creation_input_tokens",
)


class SSEUsageParser:
    """Incrementally sniff Anthropic SSE events for ``usage`` and ``model``."""

    def __init__(self) -> None:
        self.buffer = b""
        self.usage: dict[str, int] = {}
        self.model = ""

    def feed(self, chunk: bytes) -> None:
        self.buffer += chunk
        while b"\n" in self.buffer:
            line, self.buffer = self.buffer.split(b"\n", 1)
            self._handle_line(line.strip())

    def finish(self) -> None:
        self._handle_line(self.buffer.strip())
        self.buffer = b""

    def _merge_usage(self, usage: dict | None) -> None:
        if not isinstance(usage, dict):
            return
        for key in USAGE_KEYS:
            value = usage.get(key)
            if isinstance(value, (int, float)):
                self.usage[key] = max(self.usage.get(key, 0), int(value))

    def _handle_line(self, line: bytes) -> None:
        if not line.startswith(b"data:"):
            return
        payload = line[5:].strip()
        if not payload or payload == b"[DONE]":
            return
        try:
            event = json.loads(payload)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return
        if not isinstance(event, dict):
            return
        event_type = event.get("type")
        if event_type in (
            "response.created",
            "response.in_progress",
            "response.completed",
            "response.incomplete",
            "response.failed",
        ):
            response = event.get("response") or {}
            self.model = response.get("model") or self.model
            usage = response.get("usage") or {}
            if usage:
                self._merge_usage(
                    {
                        "input_tokens": usage.get("input_tokens"),
                        "output_tokens": usage.get("output_tokens"),
                        "cache_read_input_tokens": (
                            usage.get("input_tokens_details") or {}
                        ).get("cached_tokens"),
                    }
                )
        elif event_type == "message_start":
            message = event.get("message") or {}
            self.model = message.get("model") or self.model
            self._merge_usage(message.get("usage"))
        elif event_type in ("message_delta", "message"):
            self._merge_usage(event.get("usage"))
            if event_type == "message":
                self.model = event.get("model") or self.model
