"""Validation shared by signed endpoint reports; never resolve a client path on the server."""
from __future__ import annotations

import math
import re
from pathlib import PurePosixPath, PureWindowsPath

from .store import ProtectionError


def local_path(value):
    if not isinstance(value, str) or not value or len(value) > 32760:
        raise ProtectionError('本地路径无效')
    portable = value.replace(chr(92), '/')
    if any(ord(c) < 32 for c in value) or portable.startswith(('//./', '//?/')):
        raise ProtectionError('不允许设备路径、扩展设备路径或控制字符')
    windows = PureWindowsPath(value)
    posix = PurePosixPath(value)
    if not (windows.is_absolute() or posix.is_absolute()):
        raise ProtectionError('必须提供本地绝对路径')
    if '..' in windows.parts or '..' in posix.parts:
        raise ProtectionError('本地路径不允许上级目录跳转')
    if windows.is_absolute():
        if ':' in ''.join(windows.parts[1:]):
            raise ProtectionError('不允许备用数据流路径')
        for part in windows.parts[1:]:
            if part.endswith((' ', '.')) or re.fullmatch(r'(?i)(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:[.].*)?', part):
                raise ProtectionError('不允许 Windows 保留设备名或带歧义的路径')
        return str(windows)
    return str(posix)


def identity(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 256 or any(ord(c) < 32 for c in value):
        raise ProtectionError('本地文件身份无效')
    return value


def sha256(value):
    if not isinstance(value, str) or not re.fullmatch('[0-9a-fA-F]{64}', value):
        raise ProtectionError('文件 SHA-256 无效')
    return value.lower()


def timestamp(value, now):
    try:
        result = float(value)
    except (ValueError, TypeError, OverflowError):
        raise ProtectionError('事件时间无效')
    if not math.isfinite(result) or result < 0 or result > now + 300:
        raise ProtectionError('事件时间无效或设备时钟偏差过大')
    return result
