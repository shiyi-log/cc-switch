"""HTTP views: the Anthropic-compatible proxy plus a small status API."""

from __future__ import annotations

import asyncio
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
from .ccswitch import reattribute_request
from .metrics import update_account_stats
from .official import refresh_official_quotas
from .probe import probe_account, refresh_stale_probes
from .quotas import (
    apply_header_usage,
    available_accounts,
    mark_failure,
    record_budget_spend,
    release_account,
    reserve_next_account,
)
from .upstream import (
    SSEUsageParser,
    build_upstream_headers,
    calculate_cost,
    prepare_codex_body,
    upstream_url,
)

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


def _block_model(account_id, model: str) -> None:
    account = Account.objects.filter(pk=account_id).first()
    if account is None or not model:
        return
    blocked = list(account.blocked_models or [])
    if model not in blocked:
        blocked.append(model)
        account.blocked_models = blocked
        account.save(update_fields=["blocked_models"])


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
    if account is not None:
        reattribute_request(
            account=account,
            request_id=fields.get("request_id", ""),
            path=fields.get("path", ""),
            model=fields.get("model", ""),
            status_code=fields.get("status_code"),
            error_message=fields.get("blocked_reason", ""),
            input_tokens=fields.get("input_tokens", 0),
            output_tokens=fields.get("output_tokens", 0),
            cost_usd=fields.get("cost_usd"),
            duration_ms=fields.get("duration_ms"),
            ttft_ms=fields.get("ttft_ms"),
            created_at=int(time.time()),
        )


async def _record(
    account: Account,
    *,
    request_id: str,
    path: str,
    model: str,
    status_code: int,
    usage: dict | None,
    started: float,
    ttft_ms: float | None = None,
    blocked_reason: str = "",
) -> None:
    usage = usage or {}
    duration_ms = int((time.monotonic() - started) * 1000)
    ttft = int(ttft_ms) if ttft_ms is not None else duration_ms
    output_tokens = int(usage.get("output_tokens") or 0)
    tps = None
    if output_tokens and duration_ms > ttft:
        tps = output_tokens / max(0.001, (duration_ms - ttft) / 1000.0)
    cost = await sync_to_async(calculate_cost)(model, usage)
    if cost is None and account.quota_mode == Account.QUOTA_BUDGET:
        logger.warning("no ModelPrice for %r: budget spend was not recorded", model)
    if cost is not None and account.quota_mode == Account.QUOTA_BUDGET:
        await sync_to_async(record_budget_spend)(account, cost)
    total_tokens = sum(
        int(usage.get(key) or 0)
        for key in (
            "input_tokens",
            "output_tokens",
            "cache_read_input_tokens",
            "cache_creation_input_tokens",
        )
    )
    await sync_to_async(update_account_stats)(
        account.pk,
        ttft_ms=ttft,
        duration_ms=duration_ms,
        total_tokens=total_tokens,
        output_tokens=output_tokens,
        cost_usd=cost,
        success=True,
    )
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
        duration_ms=duration_ms,
        ttft_ms=ttft,
        tps=tps,
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
    wants_stream = bool(payload.get("stream"))
    protocols = _protocols_for_path(path)
    request_id = request.headers.get("x-request-id") or uuid.uuid4().hex
    started = time.monotonic()
    path_only = f"/v1/{path}"

    # Keep both quota sources fresh before routing.
    await refresh_official_quotas()
    await refresh_stale_probes()

    max_attempts = max(1, int(settings.BRIDGE.get("MAX_ATTEMPTS", 3)))
    round_delay = float(settings.BRIDGE.get("RETRY_ROUND_DELAY", 0.5))
    attempts = 0
    round_no = 0
    forced_ignore_cooldown = False
    round_excluded: set = set()
    last_error_message = ""
    last_error_status = None
    last_error_content = b""
    last_error_type = "application/json"

    while attempts < max_attempts:
        account = await sync_to_async(reserve_next_account)(
            round_excluded,
            protocols=protocols,
            model=model,
            ignore_cooldown=round_no > 0,
        )
        if account is None:
            if attempts == 0 and not forced_ignore_cooldown:
                # Everyone may be in a short pool cooldown; try one bypass
                # pass before declaring the whole pool unavailable.
                forced_ignore_cooldown = True
                round_no += 1
                continue
            if attempts == 0:
                break
            # All accounts in this round failed; start another round until the
            # global attempt budget is exhausted. Quota-exhausted accounts are
            # still filtered out, so this cannot loop forever on dead quota.
            remaining = await sync_to_async(available_accounts)(
                protocols=protocols, model=model, ignore_cooldown=True
            )
            if not remaining:
                break
            round_no += 1
            round_excluded.clear()
            await asyncio.sleep(round_delay)
            continue
        attempts += 1
        round_excluded.add(account.pk)
        handed_off = False
        try:
            forward_body = body
            if account.protocol == Account.PROTOCOL_CODEX:
                forward_body = prepare_codex_body(body)
            try:
                client, response = await _send(account, request, forward_body, path_only)
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
                    account,
                    client,
                    response,
                    request,
                    path_only,
                    request_id,
                    model,
                    started,
                    wants_stream,
                )
                # Streaming responses release the account in their generator.
                handed_off = isinstance(result, StreamingHttpResponse)
                return result

            content = await response.aread()
            status_code = response.status_code
            message = _extract_error_message(content)
            if status_code and status_code >= 400:
                logger.warning(
                    "upstream %s from %s: %s",
                    status_code,
                    account.name,
                    content[:1500].decode("utf-8", "replace"),
                )
            last_error_message = message
            await _close(client, response)

            model_unavailable = status_code == 404 and "model" in message.lower()
            if model_unavailable and model:
                await sync_to_async(_block_model)(account.pk, model)
            # Any upstream error is channel-specific as far as we are concerned:
            # try the next account instead of surfacing it immediately.
            retryable = status_code >= 400
            last_error_status = status_code
            last_error_content = content
            last_error_type = response.headers.get("content-type", "application/json")
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

    if last_error_status is not None and last_error_content:
        return HttpResponse(
            last_error_content,
            status=last_error_status,
            content_type=last_error_type,
        )

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
    now = timezone.now()
    cooling = await sync_to_async(
        lambda: Account.objects.filter(
            enabled=True, cooldown_until__gt=now
        ).count()
    )()
    if total and cooling == total:
        response = _error(
            429,
            "rate_limit_error",
            "All upstream channels are cooling down; retry shortly",
        )
        response["Retry-After"] = "3"
        return response
    return _error(
        429,
        "rate_limit_error",
        "All upstream accounts are at or beyond their 5% reserve"
        + (f" (last error: {last_error_message})" if last_error_message else ""),
    )


async def _finalize_stream(
    account, client, response, request_id, path, model, usage, first_content_at, started, status_code
):
    try:
        await _close(client, response)
        await _record(
            account,
            request_id=request_id,
            path=path,
            model=model,
            status_code=status_code,
            usage=usage,
            started=started,
            ttft_ms=(
                (first_content_at - started) * 1000 if first_content_at else None
            ),
        )
    except Exception:  # pragma: no cover - defensive
        logger.exception("stream finalizer failed")
    finally:
        await sync_to_async(release_account)(account.pk)


async def _success(
    account, client, response, request, path, request_id, model, started, wants_stream=False
):
    content_type = response.headers.get("content-type", "application/json")
    headers = _copy_headers(response)

    # Official Codex returns SSE with an application/json content-type.
    is_stream = wants_stream or "text/event-stream" in content_type
    if is_stream:
        content_type = "text/event-stream"
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
                # Run outside the cancelled request task so disconnects cannot
                # swallow the usage log / CC Switch attribution.
                asyncio.create_task(
                    _finalize_stream(
                        account,
                        client,
                        response,
                        request_id,
                        path,
                        model or parser.model,
                        parser.usage,
                        parser.first_content_at,
                        started,
                        response.status_code,
                    )
                )

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
        started=started,
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
                    "soft_limit": float(account.soft_quota_limit()),
                    "soft_reserve": account.at_soft_reserve(),
                    "reserve_ratio": float(account.reserve_ratio),
                    "inflight": account.inflight,
                    "cooldown_until": account.cooldown_until.isoformat() if account.cooldown_until else None,
                    "last_error": account.last_error,
                    "last_probe_error": account.last_probe_error,
                    "price_multiplier": float(account.price_multiplier or 1),
                    "ttft_ms": account.ewma_ttft_ms,
                    "tps": account.ewma_tps,
                    "cost_per_1k": float(account.ewma_cost_per_1k)
                    if account.ewma_cost_per_1k is not None
                    else None,
                    "success_rate": account.ewma_success,
                    "samples": account.stats_samples,
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
                    "duration_ms": log.duration_ms,
                    "ttft_ms": log.ttft_ms,
                    "tps": log.tps,
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
