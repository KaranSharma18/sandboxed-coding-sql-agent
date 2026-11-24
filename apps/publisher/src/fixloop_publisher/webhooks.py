"""Signed customer webhooks with SSRF protection (design section 15).

Signature: `Fixloop-Signature: t=<unix seconds>,v1=<hex HMAC-SHA256(secret, "<t>.<body>")>`,
the same scheme Stripe uses, so receivers can reject replays older than a few minutes.

A callback URL is untrusted input pointing *into our network*. Before every delivery the host
is resolved and every address must be public (not loopback, private, link-local, multicast or
reserved), HTTPS only, redirects are not followed, and the connection is made to the address
that was checked (so DNS cannot change between the check and the request).
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import socket
import time
from typing import Any
from urllib.parse import urlsplit

import httpx

SIGNATURE_HEADER = "Fixloop-Signature"


class UnsafeCallback(Exception):
    pass


def sign(secret: str, body: bytes, timestamp: int | None = None) -> str:
    ts = int(timestamp if timestamp is not None else time.time())
    digest = hmac.new(secret.encode(), f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
    return f"t={ts},v1={digest}"


def verify(secret: str, body: bytes, header: str, tolerance_s: int = 300) -> bool:
    """For receivers (and tests): check the signature and its age."""
    try:
        parts = dict(item.split("=", 1) for item in header.split(","))
        ts = int(parts["t"])
    except (ValueError, KeyError):
        return False
    if abs(time.time() - ts) > tolerance_s:
        return False
    expected = sign(secret, body, ts).split("v1=", 1)[1]
    return hmac.compare_digest(expected, parts.get("v1", ""))


def resolve_public(url: str, allow_private: bool = False) -> tuple[str, str, int]:
    """(host, checked IP, port) or UnsafeCallback."""
    parts = urlsplit(url)
    if parts.scheme != "https" and not allow_private:
        raise UnsafeCallback("callback URLs must use https")
    host = parts.hostname or ""
    port = parts.port or (443 if parts.scheme == "https" else 80)
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise UnsafeCallback(f"cannot resolve {host}") from exc
    addresses = sorted({str(info[4][0]) for info in infos})
    for address in addresses:
        ip = ipaddress.ip_address(address)
        if not allow_private and (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_multicast
            or ip.is_reserved
            or ip.is_unspecified
        ):
            raise UnsafeCallback(f"{host} resolves to a non-public address ({address})")
    return host, addresses[0], port


async def deliver(
    url: str,
    secret: str,
    event: str,
    data: dict[str, Any],
    *,
    timeout_s: float = 10.0,
    allow_private: bool = False,
    transport: httpx.AsyncBaseTransport | None = None,
) -> int:
    body = json.dumps(
        {"event": event, "data": data, "sent_at": int(time.time())}, separators=(",", ":")
    ).encode()
    host, address, port = resolve_public(url, allow_private)
    parts = urlsplit(url)
    headers = {
        "Content-Type": "application/json",
        SIGNATURE_HEADER: sign(secret, body),
        "User-Agent": "Fixloop-Webhooks/1",
        "Host": host if parts.port is None else f"{host}:{port}",
    }
    target = url
    extensions: dict[str, Any] = {}
    if transport is None and not allow_private:
        # Connect to the checked address; TLS still verifies the certificate for the real host name.
        target = url.replace(
            f"//{parts.netloc}", f"//{address if ':' not in address else f'[{address}]'}:{port}", 1
        )
        extensions["sni_hostname"] = host
    async with httpx.AsyncClient(timeout=timeout_s, follow_redirects=False, transport=transport) as client:
        response = await client.post(target, content=body, headers=headers, extensions=extensions)
    return response.status_code
