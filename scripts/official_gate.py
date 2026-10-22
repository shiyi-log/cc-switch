#!/usr/bin/env python3
"""Minimal official-only SSE gate.

CC Switch only fails over on HTTP status codes. The official Codex backend
reports "Selected model is at capacity" inside a 200 SSE stream, so CC Switch
never fails over. This gate buffers the first part of the stream and converts
such errors into HTTP 502, letting CC Switch switch to the next provider.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx

UPSTREAM = "https://chatgpt.com/backend-api/codex"
AUTH_FILE = Path.home() / ".codex" / "auth.json"
HOP_HEADERS = {
    "host",
    "content-length",
    "accept-encoding",
    "connection",
    "transfer-encoding",
}
CONTENT_MARKERS = (b"response.output_text.delta", b"response.content_part.added")


async def send_json(send, status: int, message: str) -> None:
    body = json.dumps(
        {"error": {"message": message, "type": "upstream_error"}}
    ).encode()
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


async def read_body(receive) -> bytes:
    chunks = []
    while True:
        message = await receive()
        if message["type"] != "http.request":
            continue
        chunks.append(message.get("body", b""))
        if not message.get("more_body"):
            return b"".join(chunks)


def wants_stream(body: bytes, content_type: str) -> bool:
    if "text/event-stream" in content_type:
        return True
    try:
        return bool(json.loads(body or b"{}").get("stream"))
    except Exception:
        return False


def upstream_path(path: str) -> str:
    if path.startswith("/v1/"):
        return path[3:]
    if path == "/v1":
        return "/"
    return path


async def app(scope, receive, send):
    if scope["type"] != "http":
        return
    path = scope.get("path", "/")
    if path in ("/health", "/"):
        await send_json(send, 200, "ok")
        return

    body = await read_body(receive)
    headers = {
        k.decode(): v.decode()
        for k, v in scope.get("headers", [])
        if k.decode().lower() not in HOP_HEADERS
    }
    # Always authenticate with the locally logged-in ChatGPT account.
    try:
        tokens = json.loads(AUTH_FILE.read_text())["tokens"]
        headers["authorization"] = f"Bearer {tokens['access_token']}"
        headers["chatgpt-account-id"] = tokens.get("account_id", "")
        headers.pop("x-api-key", None)
    except Exception:
        pass
    url = UPSTREAM + upstream_path(path)
    async with httpx.AsyncClient(timeout=httpx.Timeout(600.0, connect=20.0)) as client:
        request = client.build_request(
            scope.get("method", "POST"), url, headers=headers, content=body
        )
        try:
            response = await client.send(request, stream=True)
        except Exception as exc:  # noqa: BLE001
            await send_json(send, 502, f"official gate transport error: {exc}")
            return

        status = response.status_code
        content_type = response.headers.get("content-type", "application/json")
        if status >= 400:
            payload = await response.aread()
            await response.aclose()
            await send(
                {
                    "type": "http.response.start",
                    "status": status,
                    "headers": [
                        (b"content-type", content_type.encode()),
                        (b"content-length", str(len(payload)).encode()),
                    ],
                }
            )
            await send({"type": "http.response.body", "body": payload})
            return

        if not wants_stream(body, content_type):
            payload = await response.aread()
            await response.aclose()
            if b"capacity" in payload.lower():
                await send_json(send, 502, "official model at capacity")
                return
            await send(
                {
                    "type": "http.response.start",
                    "status": status,
                    "headers": [
                        (b"content-type", content_type.encode()),
                        (b"content-length", str(len(payload)).encode()),
                    ],
                }
            )
            await send({"type": "http.response.body", "body": payload})
            return

        iterator = response.aiter_bytes()
        buffered = b""
        rejected = False
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 3.0
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                break
            try:
                chunk = await asyncio.wait_for(iterator.__anext__(), remaining)
            except StopAsyncIteration:
                break
            except asyncio.TimeoutError:
                break
            buffered += chunk
            lowered = buffered.lower()
            if b'"type":"error"' in lowered or b'"type":"response.failed"' in lowered or b"capacity" in lowered:
                rejected = True
                break
            if any(marker in buffered for marker in CONTENT_MARKERS) or len(buffered) > 65536:
                break

        if rejected:
            await response.aclose()
            await send_json(send, 502, "official model at capacity")
            return

        await send(
            {
                "type": "http.response.start",
                "status": status,
                "headers": [
                    (b"content-type", b"text/event-stream"),
                    (b"cache-control", b"no-cache"),
                    (b"x-accel-buffering", b"no"),
                ],
            }
        )
        if buffered:
            await send({"type": "http.response.body", "body": buffered, "more_body": True})
        try:
            async for chunk in iterator:
                await send({"type": "http.response.body", "body": chunk, "more_body": True})
        finally:
            await response.aclose()
        await send({"type": "http.response.body", "body": b"", "more_body": False})
