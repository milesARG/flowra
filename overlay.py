"""Flowra on-screen indicator.

A per-pixel-alpha layered window (anti-aliased, soft shadow) that is click-through,
never takes focus, and never appears in Alt-Tab or the taskbar. Frames are drawn with
Pillow and pushed with UpdateLayeredWindow.
"""
from __future__ import annotations

import ctypes as C
import ctypes.wintypes as W
import math
import random
import time

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont

u32, g32, k32 = C.windll.user32, C.windll.gdi32, C.windll.kernel32
try:
    u32.SetProcessDpiAwarenessContext(C.c_void_p(-4))  # per-monitor v2
except Exception:
    pass

LRESULT = C.c_ssize_t
WNDPROC = C.WINFUNCTYPE(LRESULT, W.HWND, W.UINT, W.WPARAM, W.LPARAM)
u32.DefWindowProcW.argtypes = [W.HWND, W.UINT, W.WPARAM, W.LPARAM]
u32.DefWindowProcW.restype = LRESULT
u32.CreateWindowExW.restype = W.HWND
u32.CreateWindowExW.argtypes = [W.DWORD, W.LPCWSTR, W.LPCWSTR, W.DWORD, C.c_int, C.c_int, C.c_int, C.c_int,
                                W.HWND, W.HMENU, W.HINSTANCE, W.LPVOID]
u32.GetDC.restype = W.HDC
u32.GetForegroundWindow.restype = W.HWND
u32.MonitorFromWindow.restype = W.HMONITOR
u32.MonitorFromWindow.argtypes = [W.HWND, W.DWORD]
u32.GetMonitorInfoW.argtypes = [W.HMONITOR, C.c_void_p]
u32.SetWindowPos.argtypes = [W.HWND, W.HWND, C.c_int, C.c_int, C.c_int, C.c_int, W.UINT]
g32.CreateCompatibleDC.restype = W.HDC
g32.CreateCompatibleDC.argtypes = [W.HDC]
g32.CreateDIBSection.restype = W.HBITMAP
g32.CreateDIBSection.argtypes = [W.HDC, C.c_void_p, W.UINT, C.POINTER(C.c_void_p), W.HANDLE, W.DWORD]
g32.SelectObject.restype = W.HGDIOBJ
g32.SelectObject.argtypes = [W.HDC, W.HGDIOBJ]
g32.DeleteObject.argtypes = [W.HGDIOBJ]
k32.GetModuleHandleW.restype = W.HMODULE


class WNDCLASSEXW(C.Structure):
    _fields_ = [("cbSize", W.UINT), ("style", W.UINT), ("lpfnWndProc", WNDPROC), ("cbClsExtra", C.c_int),
                ("cbWndExtra", C.c_int), ("hInstance", W.HINSTANCE), ("hIcon", W.HICON), ("hCursor", W.HANDLE),
                ("hbrBackground", W.HBRUSH), ("lpszMenuName", W.LPCWSTR), ("lpszClassName", W.LPCWSTR),
                ("hIconSm", W.HICON)]


class BITMAPINFOHEADER(C.Structure):
    _fields_ = [("biSize", W.DWORD), ("biWidth", W.LONG), ("biHeight", W.LONG), ("biPlanes", W.WORD),
                ("biBitCount", W.WORD), ("biCompression", W.DWORD), ("biSizeImage", W.DWORD),
                ("biXPelsPerMeter", W.LONG), ("biYPelsPerMeter", W.LONG), ("biClrUsed", W.DWORD),
                ("biClrImportant", W.DWORD)]


class BLENDFUNCTION(C.Structure):
    _fields_ = [("BlendOp", C.c_ubyte), ("BlendFlags", C.c_ubyte),
                ("SourceConstantAlpha", C.c_ubyte), ("AlphaFormat", C.c_ubyte)]


class MONITORINFO(C.Structure):
    _fields_ = [("cbSize", W.DWORD), ("rcMonitor", W.RECT), ("rcWork", W.RECT), ("dwFlags", W.DWORD)]


u32.UpdateLayeredWindow.argtypes = [W.HWND, W.HDC, C.POINTER(W.POINT), C.POINTER(W.SIZE), W.HDC,
                                    C.POINTER(W.POINT), W.COLORREF, C.POINTER(BLENDFUNCTION), W.DWORD]

WS_EX = 0x00080000 | 0x00000020 | 0x00000008 | 0x00000080 | 0x08000000  # LAYERED|TRANSPARENT|TOPMOST|TOOLWINDOW|NOACTIVATE
WS_POPUP = 0x80000000

# ---- look ---------------------------------------------------------------------------
THEMES = {
    #            surface          ink               hairline (rgba)        shadow
    "graphite": ((18, 18, 20),    (245, 245, 247), (255, 255, 255, 30), 110),
    "light":    ((250, 250, 252), (28, 28, 30),    (0, 0, 0, 26),        70),
    "midnight": ((15, 27, 45),    (244, 235, 221), (61, 214, 196, 60),   110),
    "glass":    ((40, 42, 48),    (255, 255, 255), (255, 255, 255, 46),  90),
}
THEME_ALPHA = {"graphite": 0.93, "light": 0.96, "midnight": 0.95, "glass": 0.72, "custom": 0.94}

CANVAS_W, CANVAS_H = 250, 64   # DIP at scale 1, includes room for shadow
MARGIN = 14                    # DIP between capsule and canvas edge (shadow room)
INSET_X, INSET_Y = 18, 30      # DIP from the work-area edge to an anchor point
SNAP = 16                      # DIP: drop this close to an anchor and it snaps exactly
SS = 3
N_BARS = 13
EDIT_HINT = "Drag to place  ·  Enter to finish"

FONT_PATHS = [r"C:\Windows\Fonts\seguisb.ttf", r"C:\Windows\Fonts\segoeui.ttf"]
u32.LoadCursorW.argtypes = [W.HINSTANCE, C.c_void_p]
u32.LoadCursorW.restype = W.HANDLE
u32.SetCursor.argtypes = [W.HANDLE]
u32.MonitorFromPoint.argtypes = [W.POINT, W.DWORD]
u32.MonitorFromPoint.restype = W.HMONITOR
u32.SetWindowLongW.argtypes = [W.HWND, C.c_int, C.c_long]
u32.GetWindowLongW.argtypes = [W.HWND, C.c_int]
u32.SetCapture.argtypes = [W.HWND]


def _hex(s, default):
    try:
        s = str(s).lstrip("#")
        return tuple(int(s[i:i + 2], 16) for i in (0, 2, 4))
    except Exception:
        return default


def _mix(a, b, t):
    return tuple(int(x + (y - x) * t) for x, y in zip(a, b))


def _ease(cur, tgt, dt, speed):
    return tgt + (cur - tgt) * math.exp(-dt * speed)


def _monitor(hmon):
    mi = MONITORINFO(cbSize=C.sizeof(MONITORINFO))
    u32.GetMonitorInfoW(hmon, C.byref(mi))
    dx, dy = W.UINT(96), W.UINT(96)
    try:
        C.windll.shcore.GetDpiForMonitor(hmon, 0, C.byref(dx), C.byref(dy))
    except Exception:
        pass
    return mi.rcWork, dx.value / 96


class Overlay:
    """Placement model: one of 9 anchors on the work area + a (dx, dy) offset in DIP.
    Left-anchored capsules grow rightwards from their left edge, right-anchored ones grow
    leftwards, centre ones grow evenly. Edit mode lets you drag it and snaps to anchors."""

    def __init__(self, app):
        self.app = app
        self.running = True
        self._proc = WNDPROC(self._wndproc)
        hinst = k32.GetModuleHandleW(None)
        wc = WNDCLASSEXW(cbSize=C.sizeof(WNDCLASSEXW), lpfnWndProc=self._proc, hInstance=hinst,
                         lpszClassName="FlowraIndicator")
        u32.RegisterClassExW(C.byref(wc))
        self.hwnd = u32.CreateWindowExW(WS_EX, "FlowraIndicator", "Flowra", WS_POPUP, 0, 0, 1, 1,
                                        None, None, hinst, None)
        self.screen_dc = u32.GetDC(None)
        self.mem_dc = g32.CreateCompatibleDC(self.screen_dc)
        self.bmp = None
        self.size = (0, 0)
        self.scale = self.dpi = 1.0
        self.origin = (0, 0)
        self.ref = (0, 0)        # screen px: capsule left edge / centre / right edge (by align), vertical centre
        self.align = 0
        self.w, self.h, self.op = 30.0, 6.0, 0.0
        self.content = 0.0
        self.level = 0.0
        self.phases = [random.uniform(0, 6.28) for _ in range(N_BARS)]
        self.freqs = [random.uniform(5.0, 9.0) for _ in range(N_BARS)]
        self.prev_state = None
        self.flash_until = 0.0
        self.flash_color = None
        self.static_frames = 0
        self._base_key = None
        self._font = None
        self._dirty = True
        # edit (drag) mode
        self.editing = False
        self._edit_req = None
        self._drag = None
        self._edit_snapshot = None
        self._edit_last = 0.0
        self.edit_w = 200
        self._load_style()
        u32.ShowWindow(self.hwnd, 4)
        self._reposition()

    # ---- config -----------------------------------------------------------------------
    def _load_style(self):
        cfg = self.app.cfg
        theme = cfg.get("indicator_theme", "graphite")
        if theme == "custom":
            self.surface = _hex(cfg.get("indicator_surface"), (18, 18, 20))
            self.ink = _hex(cfg.get("indicator_ink"), (245, 245, 247))
            self.hairline, self.shadow = self.ink + (34,), 100
        else:
            self.surface, self.ink, self.hairline, self.shadow = THEMES.get(theme, THEMES["graphite"])
        if not cfg.get("indicator_shadow", True):
            self.shadow = 0
        self.fill_alpha = THEME_ALPHA.get(theme, 0.93) * float(cfg.get("indicator_opacity", 1.0))
        self.accent = _hex(cfg.get("indicator_accent", "#FF453A"), (255, 69, 58))
        self.user_scale = max(0.6, min(2.0, float(cfg.get("indicator_scale", 1.0))))
        pos = cfg.get("indicator_position", "bottom-center")
        if pos in ("bottom", "top"):
            pos += "-center"
        self.anchor = pos if pos.count("-") == 1 else "bottom-center"
        self.dx = float(cfg.get("indicator_dx", 0))
        self.dy = float(cfg.get("indicator_dy", 0))
        self.monitor_mode = cfg.get("indicator_monitor", "active")
        L = float(cfg.get("indicator_idle_length", 30))
        T = float(cfg.get("indicator_idle_thickness", 6))
        O = float(cfg.get("indicator_idle_opacity", 0.55))
        self.targets = {
            "idle": (L, T, O), "dot": (T + 2, T + 2, O), "loading": (L, T, O * 0.55),
            "holding": (96, 30, 1.0), "locked": (118, 30, 1.0), "busy": (54, 30, 1.0),
            "copied": (98, 30, 1.0), "hidden": (L, T, 0.0), "edit": (self.edit_w, 34, 1.0),
        }
        self._base_key = None

    def refresh(self):
        self._dirty = True

    # ---- placement --------------------------------------------------------------------
    def _pick_monitor(self):
        if self.editing:
            return u32.MonitorFromPoint(W.POINT(int(self.ref[0]), int(self.ref[1])), 2)
        if self.monitor_mode == "primary":
            return u32.MonitorFromPoint(W.POINT(0, 0), 1)
        if self.monitor_mode == "cursor":
            pt = W.POINT(); u32.GetCursorPos(C.byref(pt))
            return u32.MonitorFromPoint(pt, 2)
        return u32.MonitorFromWindow(u32.GetForegroundWindow(), 2)

    def _anchor_point(self, wa, dpi, anchor):
        vert, horiz = anchor.split("-")
        bx = {"left": wa.left + INSET_X * dpi, "right": wa.right - INSET_X * dpi}.get(horiz, (wa.left + wa.right) / 2)
        by = {"top": wa.top + INSET_Y * dpi, "bottom": wa.bottom - INSET_Y * dpi}.get(vert, (wa.top + wa.bottom) / 2)
        return bx, by, {"left": -1, "right": 1}.get(horiz, 0)

    def _reposition(self):
        wa, dpi = _monitor(self._pick_monitor())
        self.dpi = dpi
        self.scale = dpi * self.user_scale
        bx, by, self.align = self._anchor_point(wa, dpi, self.anchor)
        self.ref = (bx + self.dx * dpi, by + self.dy * dpi)
        cw, ch = int(CANVAS_W * self.scale), int(CANVAS_H * self.scale)
        if (cw, ch) != self.size:
            self._alloc(cw, ch)
        self._place()
        u32.SetWindowPos(self.hwnd, W.HWND(-1), 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0010)

    def _place(self):
        cw, ch = self.size
        m = MARGIN * self.scale
        rx, ry = self.ref
        x = rx - m if self.align < 0 else rx - (cw - m) if self.align > 0 else rx - cw / 2
        self.origin = (int(round(x)), int(round(ry - ch / 2)))
        self._base_key = None

    def _alloc(self, cw, ch):
        if self.bmp:
            g32.DeleteObject(self.bmp)
        bmi = BITMAPINFOHEADER(biSize=C.sizeof(BITMAPINFOHEADER), biWidth=cw, biHeight=-ch,
                               biPlanes=1, biBitCount=32, biCompression=0)
        self.bits = C.c_void_p()
        self.bmp = g32.CreateDIBSection(self.screen_dc, C.byref(bmi), 0, C.byref(self.bits), None, 0)
        g32.SelectObject(self.mem_dc, self.bmp)
        self.size = (cw, ch)

    # ---- edit (drag) mode -------------------------------------------------------------
    def begin_edit(self):
        self._edit_req = True

    def end_edit(self, save=True):
        self._edit_req = "save" if save else "revert"

    def _set_clickthrough(self, on: bool):
        ex = u32.GetWindowLongW(self.hwnd, -20)
        ex = (ex | 0x20) if on else (ex & ~0x20)
        u32.SetWindowLongW(self.hwnd, -20, ex)

    def _handle_edit_requests(self):
        req, self._edit_req = self._edit_req, None
        if req is True and not self.editing:
            cfg = self.app.cfg
            self._edit_snapshot = {k: cfg.get(k) for k in ("indicator_position", "indicator_dx", "indicator_dy")}
            font = self._get_font(int(11.5 * 3))
            self.edit_w = int(font.getlength(EDIT_HINT) / 3 + 52)
            self._load_style()
            self.editing = True
            self._edit_last = time.time()
            self._set_clickthrough(False)
            self._reposition()
        elif req in ("save", "revert") and self.editing:
            self.editing = False
            self._drag = None
            u32.ReleaseCapture()
            self._set_clickthrough(True)
            if req == "revert" and self._edit_snapshot:
                self.app.save_config(self._edit_snapshot)
            self._dirty = True

    def _settle(self):
        """Drop: pick the nearest of 9 anchors on this monitor, snap if close, save."""
        cap_w = self.w * self.scale
        rx, ry = self.ref
        cx = rx + cap_w / 2 if self.align < 0 else rx - cap_w / 2 if self.align > 0 else rx
        wa, dpi = _monitor(u32.MonitorFromPoint(W.POINT(int(cx), int(ry)), 2))
        fx = (cx - wa.left) / max(1, wa.right - wa.left)
        fy = (ry - wa.top) / max(1, wa.bottom - wa.top)
        horiz = "left" if fx < 1 / 3 else "right" if fx > 2 / 3 else "center"
        vert = "top" if fy < 1 / 3 else "bottom" if fy > 2 / 3 else "middle"
        anchor = f"{vert}-{horiz}"
        bx, by, align = self._anchor_point(wa, dpi, anchor)
        ref_x = cx - cap_w / 2 if align < 0 else cx + cap_w / 2 if align > 0 else cx
        dx, dy = (ref_x - bx) / dpi, (ry - by) / dpi
        if abs(dx) < SNAP:
            dx = 0
        if abs(dy) < SNAP:
            dy = 0
        self.anchor, self.dx, self.dy = anchor, round(dx), round(dy)
        self._reposition()
        self.app.save_config({"indicator_position": anchor, "indicator_dx": self.dx, "indicator_dy": self.dy})

    def _wndproc(self, h, m, w, l):
        try:
            if m == 0x0021:                                   # WM_MOUSEACTIVATE
                return 3                                      # MA_NOACTIVATE
            if self.editing:
                if m == 0x0020:                               # WM_SETCURSOR
                    u32.SetCursor(u32.LoadCursorW(None, C.c_void_p(32646)))  # IDC_SIZEALL
                    return 1
                if m == 0x0201:                               # LBUTTONDOWN
                    pt = W.POINT(); u32.GetCursorPos(C.byref(pt))
                    self._drag = (pt.x, pt.y, self.ref)
                    u32.SetCapture(h)
                    self._edit_last = time.time()
                    return 0
                if m == 0x0200 and self._drag:                # MOUSEMOVE
                    pt = W.POINT(); u32.GetCursorPos(C.byref(pt))
                    x0, y0, (rx, ry) = self._drag
                    self.ref = (rx + pt.x - x0, ry + pt.y - y0)
                    self._place()
                    self._edit_last = time.time()
                    return 0
                if m == 0x0202 and self._drag:                # LBUTTONUP
                    self._drag = None
                    u32.ReleaseCapture()
                    self._settle()
                    return 0
                if m == 0x0205:                               # RBUTTONUP -> done
                    self.end_edit(True)
                    return 0
        except Exception:
            pass
        return u32.DefWindowProcW(h, m, w, l)

    # ---- drawing ----------------------------------------------------------------------
    def _box(self):
        s, k = SS, self.scale
        cw, ch = self.size
        w, h = self.w * k * s, self.h * k * s
        m = MARGIN * k * s
        if self.align < 0:
            cx = m + w / 2
        elif self.align > 0:
            cx = cw * s - m - w / 2
        else:
            cx = cw * s / 2
        cy = ch * s / 2
        return [cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2], cx, cy

    def _render(self, state: str, t: float) -> Image.Image:
        s = SS
        box, cx, cy = self._box()
        a = self.op
        flashing = time.time() < self.flash_until
        edit = state == "edit"
        key = (round(self.w, 1), round(self.h, 1), round(a, 3), flashing, edit, self.size, self.align)
        if self._base_key != key:
            self._base = self._render_base(box, a, flashing, edit)
            self._base_key = key
        frame = self._base.copy()
        c = self.content * a
        if c <= 0.01:
            return frame
        x0, y0 = int(box[0] // s) * s, int(box[1] // s) * s
        x1, y1 = -(-int(box[2]) // s) * s, -(-int(box[3]) // s) * s
        layer = Image.new("RGBA", (x1 - x0, y1 - y0), (0, 0, 0, 0))
        local = [box[0] - x0, box[1] - y0, box[2] - x0, box[3] - y0]
        self._draw_content(ImageDraw.Draw(layer), state, t, c, local, cx - x0, cy - y0)
        small = layer.resize(((x1 - x0) // s, (y1 - y0) // s), Image.LANCZOS)
        frame.alpha_composite(small, (x0 // s, y0 // s))
        return frame

    def _render_base(self, box, a, flashing, edit) -> Image.Image:
        s, k = SS, self.scale
        cw, ch = self.size
        h = box[3] - box[1]
        big = Image.new("RGBA", (cw * s, ch * s), (0, 0, 0, 0))
        if self.shadow:
            sh = Image.new("RGBA", (cw, ch), (0, 0, 0, 0))
            ImageDraw.Draw(sh).rounded_rectangle(
                [b / s for b in (box[0], box[1] + 2 * k * s, box[2], box[3] + 2 * k * s)],
                radius=h / s / 2, fill=(0, 0, 0, int(self.shadow * a * min(1, self.h / 20))))
            sh = sh.filter(ImageFilter.GaussianBlur(5 * k)).resize(big.size, Image.BILINEAR)
            big.alpha_composite(sh)
        cap = Image.new("RGBA", big.size, (0, 0, 0, 0))
        cd = ImageDraw.Draw(cap)
        if flashing:
            border = self.flash_color
        elif edit:
            border = self.accent + (255,)
        else:
            border = self.hairline
        bw = max(1, int((1.5 if edit else 1) * k * s))
        cd.rounded_rectangle(box, radius=h / 2, fill=_mix(self.surface, border[:3], border[3] / 255) + (255,))
        cd.rounded_rectangle([box[0] + bw, box[1] + bw, box[2] - bw, box[3] - bw],
                             radius=max(0, h / 2 - bw), fill=self.surface + (255,))
        fa = max(0.97, self.fill_alpha) if edit else self.fill_alpha
        cap.putalpha(cap.getchannel("A").point(lambda v: int(v * fa * a)))
        big.alpha_composite(cap)
        return big.resize(self.size, Image.LANCZOS)

    def _draw_content(self, d, state, t, c, box, cx, cy):
        s, k = SS, self.scale
        u = k * s
        ink = self.ink
        if state in ("holding", "locked", "preview"):
            left = box[0] + 15 * u
            r = 3.2 * u
            breathe = 0.85 + 0.15 * math.sin(t * 4)
            d.ellipse([left - r, cy - r, left + r, cy + r], fill=self.accent + (int(255 * c * breathe),))
            bw, gap = 2.4 * u, 2.6 * u
            total = N_BARS * bw + (N_BARS - 1) * gap
            bx = left + 12 * u
            maxh, minh = 16 * u, 2.4 * u
            for i in range(N_BARS):
                env = 0.35 + 0.65 * math.sin(math.pi * (i + 0.5) / N_BARS)
                wob = 0.55 + 0.45 * math.sin(t * self.freqs[i] + self.phases[i])
                bh = minh + (maxh - minh) * self.level * env * wob
                x = bx + i * (bw + gap)
                d.rounded_rectangle([x, cy - bh / 2, x + bw, cy + bh / 2], radius=bw / 2,
                                    fill=ink + (int(235 * c),))
            if state == "locked":
                self._lock(d, bx + total + 13 * u, cy, u, ink + (int(200 * c),))
        elif state == "busy":
            for i in range(3):
                ph = (math.sin(t * 6.5 - i * 0.8) + 1) / 2
                r = 2.4 * u
                x = cx + (i - 1) * 9 * u
                d.ellipse([x - r, cy - r, x + r, cy + r], fill=ink + (int((90 + 165 * ph) * c),))
        elif state == "copied":
            font = self._get_font(int(11.5 * u))
            label = "Copied"
            tw = d.textlength(label, font=font)
            gx = cx - (tw + 16 * u) / 2
            col = ink + (int(240 * c),)
            d.line([(gx + 1 * u, cy + 0.2 * u), (gx + 4 * u, cy + 3.2 * u), (gx + 10 * u, cy - 3.6 * u)],
                   fill=col, width=max(1, int(1.8 * u)), joint="curve")
            d.text((gx + 16 * u, cy), label, font=font, fill=col, anchor="lm")
        elif state == "edit":
            font = self._get_font(int(11.5 * u))
            col = ink + (int(235 * c),)
            gx = box[0] + 18 * u
            self._move_glyph(d, gx, cy, u, self.accent + (int(255 * c),))
            d.text((gx + 14 * u, cy), EDIT_HINT, font=font, fill=col, anchor="lm")

    @staticmethod
    def _move_glyph(d, x, cy, u, col):
        lw = max(1, int(1.4 * u))
        a = 5.5 * u
        d.line([(x - a, cy), (x + a, cy)], fill=col, width=lw)
        d.line([(x, cy - a), (x, cy + a)], fill=col, width=lw)
        for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            tip = (x + dx * a, cy + dy * a)
            px, py = -dy, dx
            d.polygon([tip, (tip[0] - dx * 2.6 * u + px * 2.2 * u, tip[1] - dy * 2.6 * u + py * 2.2 * u),
                       (tip[0] - dx * 2.6 * u - px * 2.2 * u, tip[1] - dy * 2.6 * u - py * 2.2 * u)], fill=col)

    def _get_font(self, px):
        if not self._font or self._font.size != px:
            for p in FONT_PATHS:
                try:
                    self._font = ImageFont.truetype(p, px)
                    break
                except Exception:
                    continue
            else:
                self._font = ImageFont.load_default()
        return self._font

    @staticmethod
    def _lock(d, x, cy, u, col):
        bw, bh = 7 * u, 5.5 * u
        top = cy - bh / 2 + 1.8 * u
        lw = max(1, int(1.3 * u))
        sw = 4.4 * u
        d.arc([x - sw / 2, top - 4.2 * u, x + sw / 2, top + 0.6 * u], 180, 360, fill=col, width=lw)
        d.line([x - sw / 2 + lw / 2, top - 1.8 * u, x - sw / 2 + lw / 2, top + 0.5 * u], fill=col, width=lw)
        d.line([x + sw / 2 - lw / 2, top - 1.8 * u, x + sw / 2 - lw / 2, top + 0.5 * u], fill=col, width=lw)
        d.rounded_rectangle([x - bw / 2, top, x + bw / 2, top + bh], radius=1.4 * u, fill=col)

    def _push(self, img: Image.Image):
        arr = np.asarray(img, dtype=np.uint16)
        alpha = arr[..., 3:4]
        rgb = (arr[..., :3] * alpha + 127) // 255
        bgra = np.concatenate([rgb[..., ::-1], alpha], axis=2).astype(np.uint8)
        C.memmove(self.bits, bgra.ctypes.data, bgra.nbytes)
        pt, sz, src = W.POINT(*self.origin), W.SIZE(*self.size), W.POINT(0, 0)
        blend = BLENDFUNCTION(0, 0, 255, 1)
        u32.UpdateLayeredWindow(self.hwnd, self.screen_dc, C.byref(pt), C.byref(sz), self.mem_dc,
                                C.byref(src), 0, C.byref(blend), 2)

    def flash(self, rgba):
        self.flash_color, self.flash_until = rgba, time.time() + 0.9

    # ---- loop ---------------------------------------------------------------------------
    def _resolve(self, st: str) -> str:
        if self.editing:
            return "edit"
        idle_style = self.app.cfg.get("indicator_idle", "dash")
        if st in ("idle", "loading"):
            if idle_style == "hidden":
                return "hidden"
            if st == "idle" and idle_style == "dot":
                return "dot"
        return st

    def run(self):
        msg = W.MSG()
        last = time.time()
        while self.running:
            while u32.PeekMessageW(C.byref(msg), None, 0, 0, 1):
                u32.TranslateMessage(C.byref(msg))
                u32.DispatchMessageW(C.byref(msg))
            if self._edit_req is not None:
                self._handle_edit_requests()
            if self.editing and not self._drag and time.time() - self._edit_last > 45:
                self.end_edit(True)                            # forgotten edit mode closes itself
            if self._dirty and not self._drag:
                self._dirty = False
                self._load_style()
                self._reposition()
                self.static_frames = 0
            now = time.time()
            dt, last = min(0.1, now - last), now
            state = self._resolve(self.app.visual_state)
            if state != self.prev_state:
                if state in ("holding", "locked") and self.monitor_mode != "primary" \
                        and self.prev_state in (None, "idle", "dot", "loading", "hidden"):
                    self._reposition()                         # follow the monitor you're working on
                self.prev_state = state
                self.static_frames = 0
            shape = "holding" if state == "preview" else state
            tw, th, top = self.targets[shape]
            self.w = _ease(self.w, tw, dt, 16)
            self.h = _ease(self.h, th, dt, 18)
            self.op = _ease(self.op, top, dt, 14)
            want = 1.0 if th >= 20 else 0.0
            self.content = _ease(self.content, want, dt, 20 if want else 30)
            if state == "preview":
                lv = 0.45 + 0.35 * math.sin(now * 3.1) * math.sin(now * 1.7)
            else:
                lv = min(1.0, math.sqrt(self.app.rec.level * 20))
            self.level = _ease(self.level, lv, dt, 28 if lv > self.level else 9)

            animating = state in ("holding", "locked", "busy", "preview", "copied") or self._drag or \
                abs(self.w - tw) > 0.05 or abs(self.h - th) > 0.05 or abs(self.op - top) > 0.003 or \
                now < self.flash_until + 0.1
            if animating or self.static_frames < 2:
                self._push(self._render(state, now))
                self.static_frames = 0 if animating else self.static_frames + 1
                time.sleep(1 / 60 if self._drag else 1 / 50)
            else:
                time.sleep(1 / 20)
        u32.DestroyWindow(self.hwnd)

    def stop(self):
        self.running = False
