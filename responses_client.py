"""Responses-only transport. No redirects, proxies, tools or conversation reuse."""
from __future__ import annotations

import http.client
import ipaddress
import json
import os
import socket
import ssl
from urllib.parse import urlsplit, urlunsplit


class UpstreamError(RuntimeError):
    def __init__(self, message: str, retryable: bool = False):
        super().__init__(message)
        self.retryable = retryable


def responses_url(base_url: str) -> str:
    try:
        parts = urlsplit(base_url.strip())
        port = parts.port
    except (ValueError, AttributeError) as error:
        raise ValueError("base_url 格式无效") from error
    if (parts.scheme not in {"https", "http"} or not parts.hostname
            or parts.username or parts.password or parts.query or parts.fragment
            or any(c.isspace() or ord(c) < 32 for c in base_url)):
        raise ValueError("base_url 必须是 HTTP(S) API 地址，不能包含凭据、查询参数或片段")
    if port == 0:
        raise ValueError("端口无效")
    allowed = {host.strip().lower() for host in os.getenv("UPSTREAM_ALLOWED_HOSTS", "").split(",") if host.strip()}
    if allowed and parts.hostname.lower() not in allowed:
        raise ValueError("上游主机不在 UPSTREAM_ALLOWED_HOSTS 中")
    path = parts.path.rstrip("/")
    if path.endswith(("/chat/completions", "/messages")):
        raise ValueError("只支持 Responses，请填写 Base URL 或 /v1/responses 地址")
    if not path.endswith("/responses"):
        path += "/responses" if path.endswith("/v1") else "/v1/responses"
    return urlunsplit((parts.scheme, parts.netloc, path, "", ""))


def connect_upstream(parts, timeout: float):
    """Pin the validated DNS address, retaining the original TLS SNI and Host."""
    host = parts.hostname
    port = parts.port or (443 if parts.scheme == "https" else 80)
    addresses = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    allow_private = os.getenv("ALLOW_PRIVATE_UPSTREAM", "false").lower() == "true"
    if not allow_private and any(not ipaddress.ip_address(item[4][0]).is_global for item in addresses):
        raise UpstreamError("禁止访问本机、内网或保留地址的上游服务")
    if not addresses:
        raise UpstreamError("上游域名未解析到地址", True)
    connection = http.client.HTTPConnection(host, port, timeout=timeout)
    family, kind, proto, _, address = addresses[0]
    sock = socket.socket(family, kind, proto)
    try:
        sock.settimeout(timeout)
        sock.connect(address)
        if parts.scheme == "https":
            sock = ssl.create_default_context().wrap_socket(sock, server_hostname=host)
        connection.sock = sock
        return connection
    except BaseException:
        sock.close()
        raise


def request_response(base_url: str, api_key: str, model: str, prompt: str,
                     temperature: float | None = None, system_prompt: str = "",
                     timeout: float = 240) -> dict:
    parts = urlsplit(responses_url(base_url))
    body = {"model": model, "input": [{"role": "user", "content": prompt}],
            "store": False, "stream": False}
    if system_prompt:
        body["instructions"] = system_prompt
    if temperature is not None:
        body["temperature"] = temperature
    connection = None
    try:
        connection = connect_upstream(parts, timeout)
        connection.request("POST", parts.path, json.dumps(body).encode(), {
            "Authorization": f"Bearer {api_key}", "Content-Type": "application/json",
            "Accept": "application/json", "User-Agent": "ModelTrace/1.0",
        })
        response = connection.getresponse()
        # Never expose provider error bodies: some gateways echo credentials.
        if response.status != 200:
            retryable = response.status in {408, 429, 500, 502, 503, 504}
            raise UpstreamError(f"上游 HTTP {response.status}", retryable)
        raw = response.read(2 * 1024 * 1024 + 1)
        if len(raw) > 2 * 1024 * 1024:
            raise UpstreamError("上游响应超过 2 MiB 限制")
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise ValueError()
        status = payload.get("status")
        if status != "completed":
            raise UpstreamError("Responses 回答未完成或已截断", status in {"incomplete", "failed"})
        texts = []
        for item in payload.get("output", []):
            if item.get("type") != "message" or item.get("role") != "assistant":
                continue
            if item.get("status", "completed") != "completed":
                raise UpstreamError("Responses 消息未完整结束", True)
            for part in item.get("content", []):
                if part.get("type") == "refusal":
                    raise UpstreamError("模型拒绝生成，本次回答不计入", True)
                if part.get("type") == "output_text" and isinstance(part.get("text"), str):
                    texts.append(part["text"])
        if not texts:
            raise UpstreamError("Responses 未返回有效文本", True)
        return {"text": "\n".join(texts), "usage": payload.get("usage") or {}}
    except UpstreamError:
        raise
    except (socket.timeout, TimeoutError) as error:
        raise UpstreamError("上游请求超时", True) from error
    except (OSError, http.client.HTTPException) as error:
        raise UpstreamError("无法连接上游或连接中断", True) from error
    except (ValueError, KeyError, TypeError, AttributeError) as error:
        raise UpstreamError("上游未返回有效的 Responses JSON") from error
    finally:
        if connection:
            connection.close()
