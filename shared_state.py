"""Current-user protected connection information for an opted-in shared server."""

import base64
import ctypes
from ctypes import wintypes
import json
from pathlib import Path


RUNTIME = Path(__file__).with_name(".shared-runtime")
ENDPOINT_FILE = RUNTIME / "endpoint.json"
STATUS = RUNTIME / "status.json"
RESIDENT_TASK = "Codex Light TUI 共享后端（请勿结束）"


class Blob(ctypes.Structure):
    _fields_ = [("length", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_ubyte))]


def protect(data, decrypt=False):
    buffer = ctypes.create_string_buffer(data)
    source = Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    target = Blob()
    dll = ctypes.WinDLL("crypt32", use_last_error=True)
    function = dll.CryptUnprotectData if decrypt else dll.CryptProtectData
    if not function(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(target)):
        raise ctypes.WinError(ctypes.get_last_error())
    kernel = ctypes.WinDLL("kernel32")
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    try:
        return ctypes.string_at(target.data, target.length)
    finally:
        kernel.LocalFree(target.data)


def write_endpoint(url):
    protected = base64.b64encode(protect(url.encode("utf-8"))).decode("ascii")
    temporary = ENDPOINT_FILE.with_suffix(".tmp")
    temporary.write_text(json.dumps({"url": protected}), encoding="utf-8")
    temporary.replace(ENDPOINT_FILE)


def read_endpoint():
    if not ENDPOINT_FILE.is_file():
        return ""
    data = json.loads(ENDPOINT_FILE.read_text(encoding="utf-8"))
    return protect(base64.b64decode(data["url"], validate=True), decrypt=True).decode("utf-8")
