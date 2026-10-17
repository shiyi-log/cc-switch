"""HTTP views: the Anthropic-compatible proxy plus a small status API."""

from __future__ import annotations

import gzip
import hmac
import json
import zlib
import logging
import time
import uuid
from decimal import Decimal

import httpx
from asgiref.sync import sync_to_async
from django.conf import settings
from django.http import HttpResponse, JsonResponse, StreamingHttpResponse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt

from .models import Account, RequestLog
from .official import refresh_official_quotas
from .probe import probe_account, refresh_stale_probes
from .quotas import (
    apply_header_usage,
    mark_failure,
    record_budget_spend,
    release_account,
    reserve_next_account,
)
from .upstream import SSEUsageParser, build_upstream_headers, calculate_cost, upstream_url

logger = logging.getLogger("bridge")

RETRYABLE_STATUS = {401, 403, 408, 429, 500, 502, 503, 504}
HOP_BY_HOP = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
    "content-length",
    "content-encoding",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _error(status: int, error_type: str, message: str) -> JsonResponse:
    return JsonResponse(
        {"type": "error", "error": {"type": error_type, "message": message}}, status=status
    )


def _authorized(request) -> bool:
    if settings.BRIDGE.get("ALLOW_LOCALHOST_UNAUTH") and request.META.get(
        "REMOTE_ADDR"
    ) in ("127.0.0.1", "::1"):
        return True
    token = settings.BRIDGE.get("TOKEN") or ""
    if not token:
        return False
    provided = [request.headers.get("x-bridge-token")]
    authorization = request.headers.get("authorization", "")
    if authorization.lower().startswith("bearer "):
        provided.append(authorization[7:])
    provided.append(request.headers.get("x-api-key"))
    return any(value and hmac.compare_digest(value, token) for value in provided)


def _decode_body(body: bytes, encoding: str) -> bytes:
    """Decompress a request body before parsing/forwarding it upstream."""
    encoding = (encoding or "").strip().lower()
    if not encoding or encoding == "identity":
        return body
    if encoding in ("gzip", "x-gzip"):
        return gzip.decompress(body)
    if encoding == "deflate":
        return zlib.decompress(body)
    if encoding == "zstd":
        import zstandard

        return zstandard.ZstdDecompressor().decompress(
            body, max_output_size=64 * 1024 * 1024
        )
    raise ValueError(f"unsupported content-encoding: {encoding}")


async def _read_body(request) -> bytes:
    # ASGIHandler buffers the request body before invoking the view, so the
    # synchronous ``body`` property is safe (and replayable) here.
    return _decode_body(request.body, request.headers.get("content-encoding", ""))


def _protocols_for_path(path: str) -> list[str] | None:
    if path == "messages" or path.startswith("messages/"):
        return [Account.PROTOCOL_ANTHROPIC]
    if path.startswith("responses") or path.startswith("chat/completions"):
        # Official ChatGPT account first, third-party OpenAI-compatible fallback.
        return [Account.PROTOCOL_CODEX, Account.PROTOCOL_OPENAI]
    return None


def _extract_error_message(content: bytes) -> str:
    try:
        payload = json.loads(content)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return content.decode("utf-8", "replace")[:500]
    error = payload.get("error") if isinstance(payload, dict) else None
    if isinstance(error, dict):
        return str(error.get("message") or error)[:500]
    return str(payload)[:500]


def _copy_headers(response: httpx.Response) -> dict[str, str]:
    return {
        key: value
        for key, value in response.headers.items()
        if key.lower() not in HOP_BY_HOP
        and key.lower() != "content-type"
        and not key.lower().startswith("access-control-")
    }


async def _close(client: httpx.AsyncClient, response: httpx.Response) -> None:
    try:
        await response.aclose()
    finally:
        await client.aclose()


def _log_request(**fields) -> None:
    account = fields.get("account")
    if account is not None:
        fields.setdefault("account_name", account.name)
        fields.setdefault("channel", account.channel)
        fields.setdefault("tier", account.tier)
        fields.setdefault("base_url", account.base_url)
    RequestLog.objects.create(**fields)


async def _record(
    account: Account,
    *,
    request_id: str,
    path: str,
    model: str,
    status_code: int,
    usage: dict | None,
    blocked_reason: str = "",
) -> None:
    usage = usage or {}
    cost = await sync_to_async(calculate_cost)(model, usage)
    if cost is None and account.quota_mode == Account.QUOTA_BUDGET:
        logger.warning("no ModelPrice for %r: budget spend was not recorded", model)
    if cost is not None and account.quota_mode == Account.QUOTA_BUDGET:
        await sync_to_async(record_budget_spend)(account, cost)
    await sync_to_async(_log_request)(
        account=account,
        request_id=request_id,
        path=path,
        model=model,
        status_code=status_code,
        blocked_reason=blocked_reason,
        input_tokens=int(usage.get("input_tokens") or 0),
        output_tokens=int(usage.get("output_tokens") or 0),
        cache_read_tokens=int(usage.get("cache_read_input_tokens") or 0),
        cache_write_tokens=int(usage.get("cache_creation_input_tokens") or 0),
        cost_usd=cost,
    )


async def _send(account: Account, request, body: bytes, path: str):
    timeout = httpx.Timeout(float(settings.BRIDGE.get("TIMEOUT_SECONDS", 600)), connect=20.0)
    client = httpx.AsyncClient(timeout=timeout)
    url = upstream_url(account, path)
    if request.META.get("QUERY_STRING"):
        url = f"{url}?{request.META['QUERY_STRING']}"
    headers = build_upstream_headers(account, request.headers)
    upstream_request = client.build_request(request.method, url, headers=headers, content=body)
    response = await client.send(upstream_request, stream=True)
    return client, response


# ---------------------------------------------------------------------------
# Proxy
# ---------------------------------------------------------------------------
@csrf_exempt
async def proxy(request, path: str):
    if not _authorized(request):
        return _error(401, "authentication_error", "Invalid or missing bridge token")

    body = await _read_body(request)
    try:
        payload = json.loads(body) if body else {}
    except json.JSONDecodeError:
        return _error(400, "invalid_request_error", "Request body is not valid JSON")
    if not isinstance(payload, dict):
        return _error(400, "invalid_request_error", "Request body must be a JSON object")

    model = str(payload.get("model") or "")
    protocols = _protocols_for_path(path)
    request_id = request.headers.get("x-request-id") or uuid.uuid4().hex
    started = time.monotonic()
    path_only = f"/v1/{path}"

    # Keep both quota sources fresh before routing.
    await refresh_official_quotas()
    await refresh_stale_probes()

    attempted: set = set()
    max_attempts = max(1, int(settings.BRIDGE.get("MAX_ATTEMPTS", 3)))
    last_error_message = ""

    for _ in range(max_attempts):
        account = await sync_to_async(reserve_next_account)(attempted, protocols=protocols)
        if account is None:
            break
        attempted.add(account.pk)
        handed_off = False
        try:
            try:
                client, response = await _send(account, request, body, path_only)
            except Exception as exc:  # noqa: BLE001 - network failures are retried
                logger.warning("upstream request failed for %s: %s", account.name, exc)
                last_error_message = str(exc)
                await sync_to_async(mark_failure)(account.pk, str(exc))
                await sync_to_async(_log_request)(
                    account=account,
                    request_id=request_id,
                    path=path_only,
                    model=model,
                    status_code=None,
                    blocked_reason=f"transport: {exc}",
                )
                continue

            try:
                await sync_to_async(apply_header_usage)(account, response.headers)
            except Exception:  # pragma: no cover - defensive
                logger.exception("failed to persist quota headers")

            if response.status_code < 400:
                result = await _success(
                    account, client, response, request, path_only, request_id, model, started
                )
                # Streaming responses release the account in their generator.
                handed_off = isinstance(result, StreamingHttpResponse)
                return result

            content = await response.aread()
            status_code = response.status_code
            message = _extract_error_message(content)
            last_error_message = message
            await _close(client, response)

            retryable = status_code in RETRYABLE_STATUS
            if retryable:
                await sync_to_async(mark_failure)(account.pk, message, status_code)
            await sync_to_async(_log_request)(
                account=account,
                request_id=request_id,
                path=path_only,
                model=model,
                status_code=status_code,
                blocked_reason=message,
            )

            if not retryable:
                headers = _copy_headers(response)
                return HttpResponse(
                    content,
                    status=status_code,
                    content_type=response.headers.get("content-type", "application/json"),
                    headers=headers,
                )
        finally:
            if not handed_off:
                await sync_to_async(release_account)(account.pk)

    account_query = Account.objects.filter(enabled=True)
    if protocols:
        account_query = account_query.filter(protocol__in=protocols)
    total = await sync_to_async(account_query.count)()
    if total == 0:
        return _error(
            503,
            "overloaded_error",
            "No enabled upstream account is configured for protocols "
            f"{protocols or 'any'}",
        )
    return _error(
        429,
        "rate_limit_error",
        "All upstream accounts are at or beyond their 5% reserve"
        + (f" (last error: {last_error_message})" if last_error_message else ""),
    )


async def _success(account, client, response, request, path, request_id, model, started):
    content_type = response.headers.get("content-type", "application/json")
    headers = _copy_headers(response)

    if "text/event-stream" in content_type:
        parser = SSEUsageParser()

        async def stream_body():
            try:
                async for chunk in response.aiter_bytes():
                    parser.feed(chunk)
                    yield chunk
                parser.finish()
            except Exception as exc:  # noqa: BLE001 - client disconnect etc.
                logger.warning("stream aborted for %s: %s", account.name, exc)
            finally:
                try:
                    await _close(client, response)
                    await _record(
                        account,
                        request_id=request_id,
                        path=path,
                        model=model or parser.model,
                        status_code=response.status_code,
                        usage=parser.usage,
                    )
                finally:
                    await sync_to_async(release_account)(account.pk)

        streaming = StreamingHttpResponse(
            stream_body(), status=response.status_code, content_type=content_type
        )
        for key, value in headers.items():
            streaming[key] = value
        streaming["X-Accel-Buffering"] = "no"
        return streaming

    content = await response.aread()
    status_code = response.status_code
    await _close(client, response)
    usage: dict = {}
    try:
        data = json.loads(content)
        if isinstance(data, dict):
            usage = data.get("usage") or {}
            model = data.get("model") or model
    except (json.JSONDecodeError, UnicodeDecodeError):
        pass
    await _record(
        account,
        request_id=request_id,
        path=path,
        model=model,
        status_code=status_code,
        usage=usage,
    )
    return HttpResponse(content, status=status_code, content_type=content_type, headers=headers)


# ---------------------------------------------------------------------------
# Status API
# ---------------------------------------------------------------------------
def _window_payload(window) -> dict:
    return {
        "key": window.key,
        "utilization": float(window.utilization),
        "status": window.status,
        "resets_at": window.resets_at.isoformat() if window.resets_at else None,
        "cost_usd": str(window.cost_usd),
    }


@csrf_exempt
async def status(request):
    if not _authorized(request):
        return _error(401, "authentication_error", "Invalid or missing bridge token")
    if request.GET.get("refresh") in ("1", "true"):
        await refresh_stale_probes(force=True)

    def snapshot():
        accounts = list(Account.objects.prefetch_related("windows").order_by("tier", "priority"))
        now = timezone.now()
        payload = []
        for account in accounts:
            payload.append(
                {
                    "name": account.name,
                    "channel": account.channel,
                    "tier": account.tier,
                    "auth_type": account.auth_type,
                    "quota_mode": account.quota_mode,
                    "base_url": account.base_url,
                    "enabled": account.enabled,
                    "available": account.is_available(now),
                    "utilization": float(account.utilization()),
                    "remaining": float(account.remaining_ratio()),
                    "limit": float(account.quota_limit()),
                    "reserve_ratio": float(account.reserve_ratio),
                    "inflight": account.inflight,
                    "cooldown_until": account.cooldown_until.isoformat() if account.cooldown_until else None,
                    "last_error": account.last_error,
                    "last_probe_error": account.last_probe_error,
                    "windows": [_window_payload(w) for w in account.windows.all()],
                }
            )
        return payload

    return JsonResponse({"accounts": await sync_to_async(snapshot)()})


@csrf_exempt
async def logs(request):
    if not _authorized(request):
        return _error(401, "authentication_error", "Invalid or missing bridge token")

    try:
        limit = min(max(int(request.GET.get("limit", 50)), 1), 500)
    except ValueError:
        return _error(400, "invalid_request_error", "limit must be an integer")

    def snapshot():
        queryset = RequestLog.objects.select_related("account").order_by("-created_at")
        channel = request.GET.get("channel")
        account_name = request.GET.get("account")
        if channel:
            queryset = queryset.filter(channel=channel)
        if account_name:
            queryset = queryset.filter(account_name=account_name)
        rows = []
        for log in queryset[:limit]:
            rows.append(
                {
                    "time": log.created_at.isoformat(),
                    "channel": log.channel,
                    "account": log.account_name,
                    "tier": log.tier,
                    "base_url": log.base_url,
                    "path": log.path,
                    "model": log.model,
                    "status": log.status_code,
                    "blocked_reason": log.blocked_reason,
                    "input_tokens": log.input_tokens,
                    "output_tokens": log.output_tokens,
                    "cache_read_tokens": log.cache_read_tokens,
                    "cost_usd": str(log.cost_usd) if log.cost_usd is not None else None,
                }
            )
        return rows

    return JsonResponse({"logs": await sync_to_async(snapshot)()})


async def health(request):
    return JsonResponse({"status": "ok"})


@csrf_exempt
async def refresh_account(request, account_id):
    if not _authorized(request):
        return _error(401, "authentication_error", "Invalid or missing bridge token")
    try:
        account = await sync_to_async(Account.objects.get)(pk=account_id)
    except Account.DoesNotExist:
        return _error(404, "not_found_error", "Account not found")
    if account.quota_mode != Account.QUOTA_PROBE:
        return _error(400, "invalid_request_error", "Account does not use a quota probe")
    ok = await probe_account(account.pk, force=True)
    return JsonResponse({"ok": ok, "account": account.name})
