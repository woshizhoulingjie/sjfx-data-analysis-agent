"""Private Windows state: the token user and, when present, its own lowbox."""
from __future__ import annotations

import ctypes
from ctypes import wintypes
from pathlib import Path
import stat


_kernel = ctypes.WinDLL('kernel32', use_last_error=True)
_security = ctypes.WinDLL('advapi32', use_last_error=True)
_pointer = ctypes.c_void_p
_kernel.GetCurrentProcess.argtypes = []
_kernel.GetCurrentProcess.restype = wintypes.HANDLE
_kernel.CloseHandle.argtypes = [wintypes.HANDLE]
_kernel.CloseHandle.restype = wintypes.BOOL
_kernel.LocalFree.argtypes = [_pointer]
_kernel.LocalFree.restype = _pointer
_security.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD,
                                     ctypes.POINTER(wintypes.HANDLE)]
_security.OpenProcessToken.restype = wintypes.BOOL
_security.GetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int,
                                         _pointer, wintypes.DWORD,
                                         ctypes.POINTER(wintypes.DWORD)]
_security.GetTokenInformation.restype = wintypes.BOOL
_security.ConvertSidToStringSidW.argtypes = [_pointer,
                                            ctypes.POINTER(wintypes.LPWSTR)]
_security.ConvertSidToStringSidW.restype = wintypes.BOOL
_security.GetNamedSecurityInfoW.argtypes = [wintypes.LPWSTR, ctypes.c_int,
    wintypes.DWORD, ctypes.POINTER(_pointer), ctypes.POINTER(_pointer),
    ctypes.POINTER(_pointer), ctypes.POINTER(_pointer), ctypes.POINTER(_pointer)]
_security.GetNamedSecurityInfoW.restype = wintypes.DWORD
_security.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
    wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(_pointer),
    ctypes.POINTER(wintypes.DWORD)]
_security.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = wintypes.BOOL
_security.SetFileSecurityW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, _pointer]
_security.SetFileSecurityW.restype = wintypes.BOOL


def _sid_text(sid):
    text = wintypes.LPWSTR()
    if not _security.ConvertSidToStringSidW(sid, ctypes.byref(text)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return text.value
    finally:
        _kernel.LocalFree(text)


def _token_principals():
    """Read identities from this process, never from configuration or ACEs."""
    token = wintypes.HANDLE()
    if not _security.OpenProcessToken(_kernel.GetCurrentProcess(), 0x0008,
                                     ctypes.byref(token)):
        raise ctypes.WinError(ctypes.get_last_error())

    def information(kind):
        size = wintypes.DWORD()
        _security.GetTokenInformation(token, kind, None, 0, ctypes.byref(size))
        if not size.value:
            raise ctypes.WinError(ctypes.get_last_error())
        buffer = ctypes.create_string_buffer(size.value)
        if not _security.GetTokenInformation(token, kind, buffer, len(buffer),
                                             ctypes.byref(size)):
            raise ctypes.WinError(ctypes.get_last_error())
        return buffer

    try:
        user_info = information(1)  # TOKEN_USER begins with SID_AND_ATTRIBUTES.
        user = _sid_text(ctypes.cast(user_info, ctypes.POINTER(_pointer)).contents)
        flag_info = information(29)  # TokenIsAppContainer.
        is_appcontainer = ctypes.cast(flag_info, ctypes.POINTER(wintypes.DWORD)).contents.value
        appcontainer = None
        if is_appcontainer:
            app_info = information(31)  # TOKEN_APPCONTAINER_INFORMATION.Sid.
            appcontainer = _sid_text(ctypes.cast(app_info, ctypes.POINTER(_pointer)).contents)
            if not appcontainer.startswith('S-1-15-2-'):
                raise RuntimeError('Current token has no valid AppContainer SID')
        return user, appcontainer
    finally:
        _kernel.CloseHandle(token)


def current_user_sid():
    """Return the OS owner identity even inside an AppContainer."""
    return _token_principals()[0]


def protect_private_path(path):
    """Replace a current-user-owned path's DACL with exact token principals."""
    path = Path(path)
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
        raise RuntimeError('Private path cannot be a reparse point')
    user, appcontainer = _token_principals()
    owner, descriptor = _pointer(), _pointer()
    status = _security.GetNamedSecurityInfoW(str(path), 1, 1,
        ctypes.byref(owner), None, None, None, ctypes.byref(descriptor))
    if status:
        raise ctypes.WinError(status)
    try:
        if _sid_text(owner) != user:
            raise RuntimeError('Installation state belongs to another OS user')
    finally:
        _kernel.LocalFree(descriptor)

    # Construct a new protected DACL; inherited and unrelated ACEs cannot survive.
    # The normal token receives only its user ACE. A lowbox additionally needs
    # its actual own SID to pass Windows' restricted-token access check.
    principals = (user, appcontainer) if appcontainer else (user,)
    inheritance = 'OICI' if stat.S_ISDIR(info.st_mode) else ''
    sddl = 'D:P' + ''.join('(A;' + inheritance + ';FA;;;' + sid + ')'
                          for sid in principals)
    descriptor = _pointer()
    if not _security.ConvertStringSecurityDescriptorToSecurityDescriptorW(
            sddl, 1, ctypes.byref(descriptor), None):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        # DACL_SECURITY_INFORMATION | PROTECTED_DACL_SECURITY_INFORMATION.
        # Owner and mandatory integrity label remain untouched.
        if not _security.SetFileSecurityW(str(path), 0x80000004, descriptor):
            raise ctypes.WinError(ctypes.get_last_error())
    finally:
        _kernel.LocalFree(descriptor)
