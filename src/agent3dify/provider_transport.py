"""Retry individual HTTP requests without replaying an agent or its tool calls."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import time
import uuid
import json

import httpx


RETRY_STATUSES = {408, 429, 500, 502, 503, 504}
RETRY_DELAYS = (2, 5, 10, 20, 30)


def response_body_error(response: httpx.Response) -> str | None:
    """Some OpenAI-compatible proxies return errors as HTTP 200 JSON."""
    if response.is_error or response.headers.get("content-type", "").lower().startswith("text/event-stream"):
        return None
    try:
        payload = response.json()
    except (ValueError, json.JSONDecodeError):
        return None
    error = payload.get("error") if isinstance(payload, dict) else None
    message = error.get("message") if isinstance(error, dict) else None
    if not isinstance(message, str):
        return None
    lowered = message.lower()
    if any(term in lowered for term in ("overloaded", "temporarily unavailable", "try again later", "rate limit", "too many requests")):
        return message[:4000]
    return None


def retry_delay(response: httpx.Response | None, attempt: int, delays: tuple) -> float:
    if response is not None:
        value = response.headers.get("retry-after")
        if value:
            try:
                return min(60, max(0, float(value)))
            except ValueError:
                try:
                    return min(60, max(0, (parsedate_to_datetime(value) - datetime.now(timezone.utc)).total_seconds()))
                except (ValueError, TypeError):
                    pass
    return delays[attempt]


class RequestObserver:
    def __init__(self, callback=None, delays=RETRY_DELAYS):
        self.callback = callback or (lambda **fields: None)
        self.delays = delays

    def report(self, request, request_id, attempt, started, *, response=None, error=None):
        retry = attempt < len(self.delays) and (error is not None or response.status_code in RETRY_STATUSES)
        delay = retry_delay(response, attempt, self.delays) if retry else None
        self.callback(
            kind="model_request", request_id=request_id, attempt=attempt + 1,
            method=request.method, endpoint=str(request.url.copy_with(query=None)),
            status_code=response.status_code if response is not None else None,
            upstream_request_id=response.headers.get("x-request-id") or response.headers.get("request-id") if response is not None else None,
            elapsed_seconds=round(time.monotonic() - started, 3),
            error=str(error) if error is not None else response.text[:4000] if response.is_error else None,
            retry_in_seconds=delay,
        )
        return delay


class RetryAsyncTransport(httpx.AsyncBaseTransport):
    def __init__(self, callback=None, *, transport=None, delays=RETRY_DELAYS):
        self.inner = transport or httpx.AsyncHTTPTransport()
        self.observer = RequestObserver(callback, delays)

    async def handle_async_request(self, request):
        request_id = uuid.uuid4().hex
        for attempt in range(len(self.observer.delays) + 1):
            started = time.monotonic()
            response = None
            try:
                response = await self.inner.handle_async_request(request)
                # Buffer JSON completions before returning so body-read timeouts
                # are also retryable. Never replay a partially delivered SSE turn.
                if response.is_error or not response.headers.get("content-type", "").lower().startswith("text/event-stream"):
                    await response.aread()
                body_error = response_body_error(response)
            except httpx.TransportError as exc:
                if response is not None:
                    await response.aclose()
                delay = self.observer.report(request, request_id, attempt, started, response=response, error=exc)
                if delay is None:
                    raise
            else:
                delay = self.observer.report(request, request_id, attempt, started, response=response, error=body_error)
                if delay is None:
                    return response
                await response.aclose()
            await asyncio.sleep(delay)
        raise AssertionError("unreachable")

    async def aclose(self):
        await self.inner.aclose()


class RetryTransport(httpx.BaseTransport):
    def __init__(self, callback=None, *, transport=None, delays=RETRY_DELAYS):
        self.inner = transport or httpx.HTTPTransport()
        self.observer = RequestObserver(callback, delays)

    def handle_request(self, request):
        request_id = uuid.uuid4().hex
        for attempt in range(len(self.observer.delays) + 1):
            started = time.monotonic()
            response = None
            try:
                response = self.inner.handle_request(request)
                if response.is_error or not response.headers.get("content-type", "").lower().startswith("text/event-stream"):
                    response.read()
                body_error = response_body_error(response)
            except httpx.TransportError as exc:
                if response is not None:
                    response.close()
                delay = self.observer.report(request, request_id, attempt, started, response=response, error=exc)
                if delay is None:
                    raise
            else:
                delay = self.observer.report(request, request_id, attempt, started, response=response, error=body_error)
                if delay is None:
                    return response
                response.close()
            time.sleep(delay)
        raise AssertionError("unreachable")

    def close(self):
        self.inner.close()
