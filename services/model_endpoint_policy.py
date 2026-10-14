"""Exact-origin policy for administrator-selected HTTP model services.

Configuration validation is offline. Each request separately resolves and checks
every address, then connects to that address without a second DNS lookup.
"""
from __future__ import annotations

import ipaddress
import json
import re
import socket
import threading
import time
import urllib.parse

_LOOPBACK = {'127.0.0.1', 'localhost', '::1'}
_METADATA_HOSTS = {'metadata', 'metadata.google.internal', 'metadata.goog',
                   'instance-data', 'instance-data.ec2.internal'}
_METADATA_IPS = {'100.100.100.200', '168.63.129.16', 'fd00:ec2::254'}
_RESOLVER_SLOTS = threading.BoundedSemaphore(8)


def _fail(message='模型地址必须是有效 HTTP(S) 地址，且不能包含认证、查询、片段或通配符', code='configuration_error'):
    from services.model_contract import LocalModelError
    raise LocalModelError(message, code=code)


def _safe_address(value, *, loopback=False):
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return False
    # Mapped IPv4 follows the IPv4 safety rules rather than the outer IPv6 class.
    address = getattr(address, 'ipv4_mapped', None) or address
    if loopback:
        return address.is_loopback
    return not (address.is_loopback or address.is_link_local or address.is_multicast
                or address.is_unspecified or address.is_reserved
                or str(address) in _METADATA_IPS
                or (address.version == 4 and int(address) < (1 << 24)))


def _parse(raw):
    value = str(raw or '').strip()
    try:
        if not value or any(ord(c) <= 32 or ord(c) == 127 for c in value):
            raise ValueError
        parsed = urllib.parse.urlsplit(value)
        host = parsed.hostname
        if (parsed.scheme not in {'http', 'https'} or not host
                or parsed.username is not None or parsed.password is not None
                or parsed.query or parsed.fragment or '?' in value or '#' in value
                or '\\' in value or '%' in parsed.netloc or '*' in value
                or any(token in parsed.path.lower() for token in ('%2f', '%5c', '%3f', '%23'))):
            raise ValueError
        port = parsed.port
        if port is not None and not 1 <= port <= 65535:
            raise ValueError
        host = host.encode('idna').decode('ascii').lower()
        if host not in _LOOPBACK:
            try:
                ipaddress.ip_address(host)
            except ValueError:
                # Reject alternate numeric IP forms understood by OS resolvers.
                if (host.endswith('.') or len(host) > 253
                        or re.fullmatch(r'[0-9.]+|0x[0-9a-f]+', host, re.I)
                        or not all(re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', label)
                                   for label in host.split('.'))):
                    raise ValueError
                if host in _METADATA_HOSTS or host.endswith('.metadata.google.internal'):
                    raise ValueError
            else:
                if not _safe_address(host):
                    raise ValueError
        authority = '[{}]'.format(host) if ':' in host else host
        if port is not None and port != (443 if parsed.scheme == 'https' else 80):
            authority += ':' + str(port)
        origin = parsed.scheme + '://' + authority
        return parsed, host, origin
    except (ValueError, TypeError, UnicodeError):
        _fail()


def normalize_allowed_endpoints(raw=()):
    """Parse a JSON array or sequence and retain canonical exact origins only."""
    if raw is None or raw == '':
        return ()
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (ValueError, TypeError):
            _fail('模型允许端点必须是 HTTP(S) 地址的 JSON 数组')
    if not isinstance(raw, (tuple, list)) or len(raw) > 128 or any(not isinstance(item, str) for item in raw):
        _fail('模型允许端点必须是 HTTP(S) 地址的 JSON 数组，最多 128 个端点')
    return tuple(dict.fromkeys(_parse(item)[2] for item in raw))


parse_model_allowed_endpoints = normalize_allowed_endpoints


def model_url_origin(raw):
    return _parse(raw)[2]


def is_loopback_model_url(raw):
    return _parse(raw)[1] in _LOOPBACK


def validate_model_url(raw, allowed_endpoints=()):
    parsed, host, origin = _parse(raw)
    if host in _LOOPBACK:
        # Preserve the strict historical spellings and return value.
        from services.model_contract import validate_local_url
        return validate_local_url(raw)
    if origin not in normalize_allowed_endpoints(allowed_endpoints):
        _fail('远端模型地址未获管理员允许，请配置精确 HTTP(S) 端点', code='configuration_error')
    return origin + parsed.path.rstrip('/')


def resolve_model_addresses(raw, deadline):
    """Bound DNS time and return checked socket addresses for one connection."""
    parsed, host, _ = _parse(raw)
    port = parsed.port or (443 if parsed.scheme == 'https' else 80)
    loopback = host in _LOOPBACK
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        remaining = deadline - time.monotonic()
        if remaining <= 0 or not _RESOLVER_SLOTS.acquire(timeout=max(0, remaining)):
            raise TimeoutError('model DNS deadline')
        result, done = [], threading.Event()

        def resolve():
            try:
                result.append(socket.getaddrinfo(host, port, type=socket.SOCK_STREAM))
            except OSError as error:
                result.append(error)
            finally:
                _RESOLVER_SLOTS.release()
                done.set()

        threading.Thread(target=resolve, daemon=True).start()
        if not done.wait(max(0, deadline - time.monotonic())):
            raise TimeoutError('model DNS deadline')
        if isinstance(result[0], OSError):
            raise result[0]
        addresses = result[0]
    else:
        family = socket.AF_INET6 if address.version == 6 else socket.AF_INET
        addresses = [(family, socket.SOCK_STREAM, 0, '',
                      (host, port, 0, 0) if family == socket.AF_INET6 else (host, port))]
    if not addresses or any(not _safe_address(row[4][0], loopback=loopback) for row in addresses):
        _fail('模型域名解析到禁止访问的地址，已拒绝连接', code='endpoint_refused')
    return tuple(addresses)
