"""OpenAI-compatible provider configuration, kept on the server."""
from __future__ import annotations

import os
from dataclasses import dataclass
from urllib.parse import urlparse

import httpx
from langchain_openai import ChatOpenAI

from .diagnostics import sanitize
from .provider_transport import RetryAsyncTransport, RetryTransport


DEFAULT_BASE_URL = "http://110.41.3.123:37862/v1"
DEFAULT_WEB_MODEL = "gpt-5.6-sol"


def normalize_base_url(value: str) -> str:
    value = value.strip().rstrip("/")
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username or parsed.password:
        raise ValueError("请输入有效的 HTTP / HTTPS 代理地址")
    if parsed.query or parsed.fragment:
        raise ValueError("代理地址不能包含查询参数或片段")
    if parsed.path in {"", "/"}:
        value += "/v1"
    return value


@dataclass(frozen=True)
class Provider:
    base_url: str
    api_key: str
    model: str

    @classmethod
    def from_env(cls) -> Provider:
        return cls(
            normalize_base_url(os.getenv("OPENAI_BASE_URL") or os.getenv("AGENT_API_BASE_URL") or DEFAULT_BASE_URL),
            os.getenv("OPENAI_API_KEY") or os.getenv("AGENT_API_KEY") or "",
            (os.getenv("SUPERVISOR_MODEL") or DEFAULT_WEB_MODEL).removeprefix("openai:"),
        )

    def public(self) -> dict:
        return {"base_url": self.base_url, "model": self.model, "has_api_key": bool(self.api_key)}

    def chat_model(self, name: str | None = None, *, request_callback=None, clients: list | None = None) -> ChatOpenAI:
        if not self.api_key:
            raise ValueError("请先在连接设置中填写 API Key")
        timeout = httpx.Timeout(180, connect=15)
        sync_client = httpx.Client(transport=RetryTransport(request_callback), timeout=timeout, trust_env=False)
        async_client = httpx.AsyncClient(transport=RetryAsyncTransport(request_callback), timeout=timeout, trust_env=False)
        if clients is not None:
            clients.extend((sync_client, async_client))
        return ChatOpenAI(
            model=(name or self.model).removeprefix("openai:"),
            base_url=self.base_url,
            api_key=self.api_key,
            timeout=180,
            # Do not impose an output-token ceiling. The proxy/model decides its
            # context limit. The transport retries only the failed HTTP request.
            max_tokens=None,
            max_retries=0,
            # The proxy has returned overload errors inside HTTP 200 SSE streams.
            # Buffering a complete JSON response lets the transport retry that
            # failure without replaying an already-progressed agent graph.
            disable_streaming=True,
            http_client=sync_client,
            http_async_client=async_client,
            stream_usage=False,
            use_responses_api=False,
        )

    def list_models(self) -> list[str]:
        with httpx.Client(timeout=25, trust_env=False) as client:
            response = client.get(f"{self.base_url}/models", headers={"Authorization": f"Bearer {self.api_key}"})
            response.raise_for_status()
            return sorted(item["id"] for item in response.json().get("data", []) if isinstance(item.get("id"), str))

    def redact(self, value: str) -> str:
        return sanitize(value, self.api_key)
