"""Fetch a URL and extract readable text content."""

import ipaddress
import os
import re
import socket
from urllib.parse import urlparse

import httpx

FETCH_TIMEOUT = int(os.getenv("WEB_FETCH_TIMEOUT", "15"))
FETCH_MAX_CHARS = int(os.getenv("WEB_FETCH_MAX_CHARS", "10000"))

_BLOCKED_NETWORKS = [
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),
    ipaddress.ip_network("fe80::/10"),
]

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\n{3,}")
_SCRIPT_STYLE_RE = re.compile(
    r"<(script|style)[^>]*>.*?</\1>", re.DOTALL | re.IGNORECASE
)


def _is_private_host(hostname: str) -> bool:
    try:
        addrinfo = socket.getaddrinfo(hostname, None, socket.AF_UNSPEC,
                                      socket.SOCK_STREAM)
        for _family, _type, _proto, _cn, sockaddr in addrinfo:
            ip = ipaddress.ip_address(sockaddr[0])
            for net in _BLOCKED_NETWORKS:
                if ip in net:
                    return True
    except socket.gaierror:
        return True
    return False


def _html_to_text(html: str) -> str:
    html = _SCRIPT_STYLE_RE.sub("", html)
    text = _TAG_RE.sub("", html)
    for entity, char in [
        ("&amp;", "&"), ("&lt;", "<"), ("&gt;", ">"),
        ("&quot;", '"'), ("&#39;", "'"), ("&nbsp;", " ")
    ]:
        text = text.replace(entity, char)
    return _WS_RE.sub("\n\n", text).strip()


def _extract_title(html: str) -> str:
    m = re.search(r"<title[^>]*>(.*?)</title>", html, re.DOTALL | re.IGNORECASE)
    return m.group(1).strip() if m else ""


def web_fetch(url: str, max_chars: int | None = None, *,
              workspace_root: str = "") -> dict:
    if max_chars is None:
        max_chars = FETCH_MAX_CHARS

    parsed = urlparse(url)
    hostname = parsed.hostname
    if not hostname:
        return {"error": f"Invalid URL: {url}"}
    if parsed.scheme not in ("http", "https"):
        return {"error": f"Unsupported scheme: {parsed.scheme}"}
    if _is_private_host(hostname):
        return {"error": f"Blocked: private/internal IP for {hostname}"}

    try:
        resp = httpx.get(url, timeout=FETCH_TIMEOUT, follow_redirects=True,
                         headers={"User-Agent": "Stepflow/1.0"})
        resp.raise_for_status()
    except httpx.TimeoutException:
        return {"error": f"Fetch timed out after {FETCH_TIMEOUT}s", "url": url}
    except Exception as e:
        return {"error": f"Fetch failed: {e}", "url": url}

    content_type = resp.headers.get("content-type", "")
    raw = resp.text
    title = _extract_title(raw) if "text/html" in content_type else ""
    content = _html_to_text(raw) if "text/html" in content_type else raw

    truncated = len(content) > max_chars
    if truncated:
        content = content[:max_chars] + "\n\n... [truncated]"

    return {
        "url": url, "title": title, "content": content,
        "content_length": len(content), "truncated": truncated,
    }
