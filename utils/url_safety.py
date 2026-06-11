"""
SSRF-защита для загрузок по URL, полученным от пользователя или внешних API.

Проверяет схему (только http/https) и то, что все IP-адреса, в которые
резолвится хост, являются публичными (не loopback/private/link-local/reserved).
Опционально ограничивает домены allowlist'ом.
"""
import asyncio
import ipaddress
import logging
import socket
from urllib.parse import urlparse

logger = logging.getLogger(__name__)


def _ip_is_public(ip_str: str) -> bool:
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        return False
    return not (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


def _resolve_all_public(host: str) -> bool:
    """True, только если хост резолвится и ВСЕ его адреса публичные."""
    try:
        infos = socket.getaddrinfo(host, None)
    except Exception as exc:
        logger.debug("DNS resolution failed for %s: %s", host, exc)
        return False
    if not infos:
        return False
    for info in infos:
        ip_str = info[4][0]
        if not _ip_is_public(ip_str):
            logger.warning("SSRF guard: host %s резолвится в непубличный адрес %s", host, ip_str)
            return False
    return True


def is_safe_public_url(url: str, allowed_hosts: set[str] | None = None) -> bool:
    """
    Проверяет, что URL безопасен для серверной загрузки.

    - схема строго http/https;
    - если задан ``allowed_hosts`` — хост (или его поддомен) должен в нём быть;
    - все IP, в которые резолвится хост, должны быть публичными.
    """
    try:
        parsed = urlparse((url or "").strip())
    except Exception:
        return False

    if parsed.scheme not in ("http", "https"):
        return False

    host = parsed.hostname
    if not host:
        return False

    if allowed_hosts is not None:
        host_l = host.lower()
        if not any(host_l == h or host_l.endswith("." + h) for h in allowed_hosts):
            logger.warning("SSRF guard: host %s не в allowlist", host)
            return False

    # Хост-литерал IP — проверяем напрямую, без DNS.
    try:
        ipaddress.ip_address(host)
        return _ip_is_public(host)
    except ValueError:
        pass

    return _resolve_all_public(host)


async def is_safe_public_url_async(url: str, allowed_hosts: set[str] | None = None) -> bool:
    """Async-обёртка: резолвинг DNS выполняется в отдельном потоке."""
    return await asyncio.to_thread(is_safe_public_url, url, allowed_hosts)
