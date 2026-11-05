"""Decide where dictated text should go: paste into the focused field, or clipboard only.

Rule of thumb: a wrong "clipboard" is a mild annoyance (you press Ctrl+V yourself), a wrong
"paste" loses the text into nowhere. So we only choose clipboard when we're confident there
is no text field; anything uncertain -> paste.
"""
from __future__ import annotations

import ctypes as C
import ctypes.wintypes as W
import threading
from dataclasses import dataclass
from pathlib import Path

u32, k32, adv = C.windll.user32, C.windll.kernel32, C.windll.advapi32
u32.GetForegroundWindow.restype = W.HWND
k32.OpenProcess.restype = W.HANDLE

NO_TEXT_CLASSES = {"Progman", "WorkerW", "Shell_TrayWnd", "Shell_SecondaryTrayWnd",
                   "NotifyIconOverflowWindow", "TopLevelWindowForOverflowXamlIsland"}
# UIA control types
EDIT, DOCUMENT, COMBO = 50004, 50030, 50003
NOT_TEXT = {50000: "Button", 50002: "CheckBox", 50005: "Hyperlink", 50006: "Image",
            50007: "ListItem", 50008: "List", 50011: "MenuItem", 50013: "RadioButton",
            50015: "Slider", 50019: "TabItem", 50023: "Tree", 50024: "TreeItem",
            50036: "DataGrid", 50029: "DataItem", 50031: "SplitButton"}


class GUITHREADINFO(C.Structure):
    _fields_ = [("cbSize", W.DWORD), ("flags", W.DWORD), ("hwndActive", W.HWND), ("hwndFocus", W.HWND),
                ("hwndCapture", W.HWND), ("hwndMenuOwner", W.HWND), ("hwndMoveSize", W.HWND),
                ("hwndCaret", W.HWND), ("rcCaret", W.RECT)]


@dataclass
class Target:
    action: str        # "paste" | "clipboard"
    reason: str
    exe: str


def _process_info(hwnd):
    pid = W.DWORD()
    tid = u32.GetWindowThreadProcessId(hwnd, C.byref(pid))
    h = k32.OpenProcess(0x1000, False, pid.value)  # PROCESS_QUERY_LIMITED_INFORMATION
    exe, elevated = "", None
    if h:
        buf = C.create_unicode_buffer(1024)
        size = W.DWORD(1024)
        if k32.QueryFullProcessImageNameW(h, 0, buf, C.byref(size)):
            exe = Path(buf.value).name.lower()
        tok = W.HANDLE()
        if adv.OpenProcessToken(h, 0x0008, C.byref(tok)):  # TOKEN_QUERY
            val, ret = W.DWORD(), W.DWORD()
            if adv.GetTokenInformation(tok, 20, C.byref(val), 4, C.byref(ret)):  # TokenElevation
                elevated = bool(val.value)
            k32.CloseHandle(tok)
        else:
            elevated = True  # can't open its token as a normal user -> almost certainly elevated
        k32.CloseHandle(h)
    return exe, elevated, tid


def _we_are_elevated() -> bool:
    try:
        return bool(C.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


_uia = None
_uia_lock = threading.Lock()


def warm_up():
    """Generate comtypes UIA wrappers once (slow the very first time)."""
    try:
        import comtypes.client as cc
        cc.GetModule("UIAutomationCore.dll")
    except Exception:
        pass


def _uia_focus(result: dict):
    try:
        import comtypes
        import comtypes.client as cc
        comtypes.CoInitialize()
        try:
            from comtypes.gen.UIAutomationClient import (CUIAutomation, IUIAutomation,
                                                          IUIAutomationValuePattern)
            uia = cc.CreateObject(CUIAutomation, interface=IUIAutomation)
            el = uia.GetFocusedElement()
            result["type"] = el.CurrentControlType
            result["cls"] = el.CurrentClassName
            try:
                vp = el.GetCurrentPattern(10002)  # ValuePattern
                if vp:
                    result["readonly"] = bool(vp.QueryInterface(IUIAutomationValuePattern).CurrentIsReadOnly)
            except Exception:
                pass
        finally:
            comtypes.CoUninitialize()
    except Exception as e:
        result["error"] = str(e)


def detect(app_rules: dict | None = None, timeout: float = 0.15) -> Target:
    hwnd = u32.GetForegroundWindow()
    if not hwnd:
        return Target("clipboard", "no active window", "")
    cls = C.create_unicode_buffer(256)
    u32.GetClassNameW(hwnd, cls, 256)
    exe, elevated, tid = _process_info(hwnd)

    rule = (app_rules or {}).get(exe)
    if rule == "clipboard":
        return Target("clipboard", "app rule", exe)
    if rule == "paste":
        return Target("paste", "app rule", exe)

    if cls.value in NO_TEXT_CLASSES:
        return Target("clipboard", f"desktop/taskbar ({cls.value})", exe)
    if elevated and not _we_are_elevated():
        return Target("clipboard", "admin window blocks paste", exe)

    gti = GUITHREADINFO(cbSize=C.sizeof(GUITHREADINFO))
    if u32.GetGUIThreadInfo(tid, C.byref(gti)) and gti.hwndCaret:
        return Target("paste", "text caret", exe)

    res: dict = {}
    t = threading.Thread(target=_uia_focus, args=(res,), daemon=True)
    t.start()
    t.join(timeout)
    if t.is_alive() or "type" not in res:
        return Target("paste", "unknown (default)", exe)
    ctype = res["type"]
    if ctype in (EDIT, DOCUMENT, COMBO):
        if res.get("readonly") and ctype == EDIT:
            return Target("clipboard", "read-only field", exe)
        return Target("paste", "text field", exe)
    if ctype in NOT_TEXT:
        return Target("clipboard", f"focus on {NOT_TEXT[ctype]}", exe)
    if exe == "explorer.exe" and ctype in (50033, 50032):  # Pane / Window in Explorer
        return Target("clipboard", "Explorer", exe)
    return Target("paste", f"uncertain (type {ctype})", exe)


if __name__ == "__main__":
    import time
    warm_up()
    time.sleep(2)
    t0 = time.time()
    print(detect(), f"{(time.time() - t0) * 1000:.0f} ms")
