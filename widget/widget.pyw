"""A floating, always-on-top ring for Claude's session limit.

Reads what the session-limit plugin writes in this folder:
  usage.json          the rate-limit windows and the 5-hour window's history
  sessions/<id>.json  each session's state: working or not, context fill
and shows how much of the 5-hour limit is left, plays a continuous siren whose
pitch rises as it runs out, spins while Claude works and chimes when a long
reply is done. On hover it opens a card: a usage graph, a forecast of when the
limit runs out at the current pace, the weekly limit and the chat's context.
Drag it anywhere; double-click to mute the siren; right-click for options.
One instance at a time.
"""

import collections
import ctypes
import json
import math
import re
import shutil
import subprocess
import sys
import threading
import time
import tkinter as tk
import traceback
import winreg
from ctypes import wintypes
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont

HERE = Path(__file__).resolve().parent
# What the plugin writes and the ring keeps, in one place whichever copy runs
# (this folder, or the copy Claude installs under plugins/cache).
DATA = Path.home() / ".claude" / "session-limit"
USAGE = DATA / "usage.json"
SESSIONS = DATA / "sessions"
STATE = DATA / "state.json"
LOG = DATA / "widget.log"
CODEX_HISTORY = DATA / "codex.json"


def move_old_data():
    """Brings over what earlier versions kept beside this file."""
    DATA.mkdir(parents=True, exist_ok=True)
    for old in [HERE / "usage.json", HERE / "state.json", *HERE.glob("sessions/*.json")]:
        new = DATA / old.relative_to(HERE)
        try:
            if not new.exists():
                new.parent.mkdir(parents=True, exist_ok=True)
                old.replace(new)
        except OSError:
            pass


move_old_data()

SHOW = "--show" in sys.argv  # opened by hand (the desktop shortcut): the ring shows, even if hidden
WM_SHOW_RING = 0x8000 + 2      # what a second launch sends the running one

WINDOW = 5 * 3600   # the session limit's window, in seconds
CHIME_AFTER = 15    # seconds a reply must take for its end to chime
STALE = 90          # seconds without a heartbeat before "working" is ignored
PACE = 45 * 60      # how far back the forecast measures the pace
OLD = 20 * 60       # seconds after which the figure may be out of date (claude.ai use, say)

GREEN, AMBER, RED, GREY = (52, 199, 89), (255, 179, 0), (255, 69, 58), (142, 142, 147)
CLAUDE = (217, 119, 87)
TEAL = (16, 163, 127)  # the Codex ring's color while plenty is left
CODEX_COMET = (130, 235, 205)  # its spinner while Codex works

CODEX_LOGS = Path.home() / ".codex" / "sessions"  # Codex writes its limits into these
CODEX_STATE = Path.home() / ".codex" / ".codex-global-state.json"  # where the Codex app lists its SSH servers
WHITE, SOFT, DIM = (255, 255, 255), (190, 190, 196), (130, 130, 136)

BASE = 124   # ring size in 96-dpi pixels
SS = 3       # supersampling for smooth edges
FRAMES = 24  # spinner frames per turn

# Every word the ring, its card, its icon and its menu show, in Arabic and English.
LANG = ["ar"]
STRINGS = {
    "title": ("حدّ الجلسة", "Session limit"),
    "left_word": ("متبقية", "left"),
    "no_reading": ("لا توجد قراءة بعد", "No reading yet"),
    "resets_at": ("يتجدّد الحدّ الساعة {t}، بعد {span}", "Resets at {t}, in {span}"),
    "resets_on": ("يتجدّد الحدّ يوم {day} الساعة {t}، بعد {span}", "Resets {day} at {t}, in {span}"),
    "reset_done": ("تجدّد الحدّ، ولا يوجد استهلاك جديد بعد", "The limit reset; nothing used since"),
    "fc_wait": ("يظهر التوقّع بعد بضع دقائق من الاستخدام", "A forecast after a few minutes of use"),
    "fc_early": ("بهذا المعدّل ينفد الحدّ {when}، قبل التجديد {by}",
                 "At this pace it runs out {when}, {span} before the reset"),
    "fc_ok": ("بهذا المعدّل يكفيك حتى التجديد، ويتبقّى نحو {p}",
              "At this pace it lasts until the reset, with about {p} to spare"),
    "fc_early_week": ("بمعدّل هذا الأسبوع ينفد الحدّ {when}، قبل التجديد {by}",
                      "At this week's rate it runs out {when}, {span} before the reset"),
    "fc_ok_week": ("بمعدّل هذا الأسبوع يكفيك حتى التجديد، ويتبقّى نحو {p}",
                   "At this week's rate it lasts until the reset, with about {p} to spare"),
    "at_time": ("الساعة {t}", "at {t}"),
    "on_day": ("يوم {day} الساعة {t}", "on {day} at {t}"),
    "weekly": ("الحدّ الأسبوعي", "Weekly limit"),
    "weekly_left": ("{p} متبقية", "{p} left"),
    "weekly_resets": ("يتجدّد يوم {day} الساعة {t}", "Resets {day} at {t}"),
    "context": ("ذاكرة المحادثة", "Chat context"),
    "context_used": ("{p} مستخدمة", "{p} used"),
    "working": ("يعمل الآن · {m}:{s}", "Working · {m}:{s}"),
    "ready": ("جاهز", "Ready"),
    "siren": ("صوت الإنذار", "Siren"),
    "muted": ("صامت", "muted"),
    "updated": ("آخر تحديث", "Last update"),
    "just_now": ("الآن", "Just now"),
    "ago": ("قبل {span}", "{span} ago"),
    "old_note": ("قد يكون الرقم قديمًا إن استخدمت claude.ai منذ ذلك الحين",
                 "May be out of date if you used claude.ai since"),
    "old_note_codex": ("قد يكون الرقم قديمًا؛ يتحدّث عند استخدام Codex",
                       "May be out of date; it updates when you use Codex"),
    "ring_out": ("ينفد {t}", "out {t}"),
    "ring_renew": ("يتجدّد {t}", "resets {t}"),
    "ring_reset": ("تجدّد", "reset"),
    "vol": ("الصوت {p}", "Volume {p}"),
    "mute": ("كتم صوت الإنذار", "Mute the siren"),
    "unmute": ("تشغيل صوت الإنذار", "Unmute the siren"),
    "volume": ("مستوى الصوت", "Volume"),
    "chime": ("تنبيه صوتي عند انتهاء Claude", "Chime when Claude finishes"),
    "preview": ("استماع", "Preview"),
    "siren_at": ("الإنذار عند {p}%", "Siren at {p}%"),
    "chime_test": ("صوت التنبيه", "Chime"),
    "show_ring": ("إظهار الدائرة", "Show the ring"),
    "startup": ("التشغيل مع بدء ويندوز", "Start with Windows"),
    "quit": ("إغلاق", "Quit"),
    "refresh_now": ("تحديث الآن", "Refresh now"),
    "refreshing": ("جارٍ التحديث…", "Refreshing…"),
    "refreshed": ("تمّ التحديث", "Updated"),
    "no_session": ("لا توجد جلسة", "No session"),
    "plan": ("الخطة", "Plan"),
    "busy": ("يعمل الآن", "Working"),
    "busy_on": ("يعمل الآن على {where}", "Working on {where}"),
    "codex_remote": ("متابعة Codex على الخوادم", "Follow Codex on servers"),
}
DAYS = {"ar": ["الإثنين", "الثلاثاء", "الأربعاء", "الخميس", "الجمعة", "السبت", "الأحد"],
        "en": ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]}


def T(key, **values):
    ar, en = STRINGS[key]
    return (ar if LANG[0] == "ar" else en).format(**values)

user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32
kernel32 = ctypes.windll.kernel32


def log(msg):
    try:
        with LOG.open("a", encoding="utf-8") as f:
            f.write(f"{datetime.now():%Y-%m-%d %H:%M:%S} {msg}\n")
    except OSError:
        pass


# --- one instance ----------------------------------------------------------

kernel32.CreateMutexW.restype = wintypes.HANDLE
_mutex = kernel32.CreateMutexW(None, False, "Local\\ClaudeSessionLimitWidget")
if kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS: the running one shows its ring instead
    if SHOW:
        user32.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
        user32.FindWindowW.restype = wintypes.HWND
        user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        running = user32.FindWindowW("ClaudeSessionLimitTray", None)
        if running:
            user32.PostMessageW(running, WM_SHOW_RING, 0, 0)
    sys.exit(0)

try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    user32.SetProcessDPIAware()


# --- per-pixel-alpha windows (UpdateLayeredWindow) and Windows text ---------

class BLENDFUNCTION(ctypes.Structure):
    _fields_ = [("BlendOp", ctypes.c_ubyte), ("BlendFlags", ctypes.c_ubyte),
                ("SourceConstantAlpha", ctypes.c_ubyte), ("AlphaFormat", ctypes.c_ubyte)]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG), ("biHeight", wintypes.LONG),
                ("biPlanes", wintypes.WORD), ("biBitCount", wintypes.WORD),
                ("biCompression", wintypes.DWORD), ("biSizeImage", wintypes.DWORD),
                ("biXPelsPerMeter", wintypes.LONG), ("biYPelsPerMeter", wintypes.LONG),
                ("biClrUsed", wintypes.DWORD), ("biClrImportant", wintypes.DWORD)]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", wintypes.DWORD * 3)]


class MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT),
                ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD)]


user32.GetDC.argtypes = [wintypes.HWND]
user32.GetDC.restype = wintypes.HDC
user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
user32.GetParent.argtypes = [wintypes.HWND]
user32.GetParent.restype = wintypes.HWND
user32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
user32.GetWindowLongW.restype = wintypes.LONG
user32.SetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.LONG]
user32.UpdateLayeredWindow.argtypes = [
    wintypes.HWND, wintypes.HDC, ctypes.POINTER(wintypes.POINT), ctypes.POINTER(wintypes.SIZE),
    wintypes.HDC, ctypes.POINTER(wintypes.POINT), wintypes.COLORREF,
    ctypes.POINTER(BLENDFUNCTION), wintypes.DWORD]
user32.MonitorFromWindow.argtypes = [wintypes.HWND, wintypes.DWORD]
user32.MonitorFromWindow.restype = wintypes.HMONITOR
user32.GetMonitorInfoW.argtypes = [wintypes.HMONITOR, ctypes.POINTER(MONITORINFO)]
gdi32.ExtTextOutW.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int, wintypes.UINT,
                              ctypes.POINTER(wintypes.RECT), wintypes.LPCWSTR, wintypes.UINT, ctypes.c_void_p]
gdi32.GetTextExtentPoint32W.argtypes = [wintypes.HDC, wintypes.LPCWSTR, ctypes.c_int,
                                        ctypes.POINTER(wintypes.SIZE)]
gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
gdi32.CreateCompatibleDC.restype = wintypes.HDC
gdi32.CreateDIBSection.argtypes = [wintypes.HDC, ctypes.c_void_p, wintypes.UINT,
                                   ctypes.POINTER(ctypes.c_void_p), wintypes.HANDLE, wintypes.DWORD]
gdi32.CreateDIBSection.restype = wintypes.HBITMAP
gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
gdi32.SelectObject.restype = wintypes.HGDIOBJ
gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
gdi32.DeleteDC.argtypes = [wintypes.HDC]
gdi32.CreateFontW.argtypes = [ctypes.c_int] * 5 + [wintypes.DWORD] * 8 + [wintypes.LPCWSTR]
gdi32.CreateFontW.restype = wintypes.HFONT
gdi32.SetTextColor.argtypes = [wintypes.HDC, wintypes.COLORREF]
gdi32.SetBkMode.argtypes = [wintypes.HDC, ctypes.c_int]

class GUID(ctypes.Structure):
    _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD), ("Data3", wintypes.WORD),
                ("Data4", ctypes.c_ubyte * 8)]


class NOTIFYICONDATAW(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("hWnd", wintypes.HWND), ("uID", wintypes.UINT),
                ("uFlags", wintypes.UINT), ("uCallbackMessage", wintypes.UINT), ("hIcon", wintypes.HICON),
                ("szTip", wintypes.WCHAR * 128), ("dwState", wintypes.DWORD), ("dwStateMask", wintypes.DWORD),
                ("szInfo", wintypes.WCHAR * 256), ("uVersion", wintypes.UINT),
                ("szInfoTitle", wintypes.WCHAR * 64), ("dwInfoFlags", wintypes.DWORD),
                ("guidItem", GUID), ("hBalloonIcon", wintypes.HICON)]


class ICONINFO(ctypes.Structure):
    _fields_ = [("fIcon", wintypes.BOOL), ("xHotspot", wintypes.DWORD), ("yHotspot", wintypes.DWORD),
                ("hbmMask", wintypes.HBITMAP), ("hbmColor", wintypes.HBITMAP)]


WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)


class WNDCLASSW(ctypes.Structure):
    _fields_ = [("style", wintypes.UINT), ("lpfnWndProc", WNDPROC), ("cbClsExtra", ctypes.c_int),
                ("cbWndExtra", ctypes.c_int), ("hInstance", wintypes.HINSTANCE), ("hIcon", wintypes.HICON),
                ("hCursor", wintypes.HANDLE), ("hbrBackground", wintypes.HBRUSH),
                ("lpszMenuName", wintypes.LPCWSTR), ("lpszClassName", wintypes.LPCWSTR)]


shell32 = ctypes.windll.shell32
shell32.Shell_NotifyIconW.argtypes = [wintypes.DWORD, ctypes.POINTER(NOTIFYICONDATAW)]
shell32.Shell_NotifyIconW.restype = wintypes.BOOL
user32.CreateIconIndirect.argtypes = [ctypes.POINTER(ICONINFO)]
user32.CreateIconIndirect.restype = wintypes.HICON
user32.DestroyIcon.argtypes = [wintypes.HICON]
user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.DefWindowProcW.restype = ctypes.c_ssize_t
user32.RegisterClassW.argtypes = [ctypes.POINTER(WNDCLASSW)]
user32.RegisterClassW.restype = wintypes.ATOM
user32.CreateWindowExW.argtypes = [wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
                                   ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.HWND,
                                   wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID]
user32.CreateWindowExW.restype = wintypes.HWND
user32.RegisterWindowMessageW.argtypes = [wintypes.LPCWSTR]
user32.RegisterWindowMessageW.restype = wintypes.UINT
user32.SetForegroundWindow.argtypes = [wintypes.HWND]
user32.MonitorFromPoint.argtypes = [wintypes.POINT, wintypes.DWORD]
user32.MonitorFromPoint.restype = wintypes.HMONITOR
kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
kernel32.GetModuleHandleW.restype = wintypes.HMODULE
gdi32.CreateBitmap.argtypes = [ctypes.c_int, ctypes.c_int, wintypes.UINT, wintypes.UINT, ctypes.c_void_p]
gdi32.CreateBitmap.restype = wintypes.HBITMAP

NIM_ADD, NIM_MODIFY, NIM_DELETE, NIM_SETVERSION = 0, 1, 2, 4
NIF_MESSAGE, NIF_ICON, NIF_TIP = 0x1, 0x2, 0x4
NIN_SELECT, NIN_KEYSELECT, NIN_POPUPOPEN, NIN_POPUPCLOSE = 0x400, 0x401, 0x406, 0x407
WM_CONTEXTMENU, WM_APP = 0x7B, 0x8000

GWL_EXSTYLE = -20
WS_EX_LAYERED, WS_EX_TOOLWINDOW, WS_EX_TRANSPARENT, WS_EX_NOACTIVATE = 0x80000, 0x80, 0x20, 0x08000000
ETO_RTLREADING, ETO_NUMERICSLATIN = 0x80, 0x800


def make_layered(hwnd, extra=0):
    style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
    user32.SetWindowLongW(hwnd, GWL_EXSTYLE, style | WS_EX_LAYERED | WS_EX_TOOLWINDOW | extra)


def dib(dc, w, h):
    """A top-down 32-bit bitmap and the address of its pixels."""
    bmi = BITMAPINFO()
    bmi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
    bmi.bmiHeader.biWidth, bmi.bmiHeader.biHeight = w, -h
    bmi.bmiHeader.biPlanes, bmi.bmiHeader.biBitCount = 1, 32
    bits = ctypes.c_void_p()
    bmp = gdi32.CreateDIBSection(dc, ctypes.byref(bmi), 0, ctypes.byref(bits), None, 0)
    return bmp, bits


def blit(hwnd, img):
    """Shows an RGBA image as the window's whole content, alpha included."""
    w, h = img.size
    a = np.asarray(img, dtype=np.uint16)
    rgb = a[..., :3] * a[..., 3:4] // 255  # premultiplied, as Windows wants
    data = np.dstack([rgb[..., 2], rgb[..., 1], rgb[..., 0], a[..., 3]]).astype(np.uint8).tobytes()

    screen = user32.GetDC(None)
    mem = gdi32.CreateCompatibleDC(screen)
    bmp, bits = dib(screen, w, h)
    ctypes.memmove(bits, data, len(data))
    old = gdi32.SelectObject(mem, bmp)
    blend = BLENDFUNCTION(0, 0, 255, 1)  # AC_SRC_OVER, AC_SRC_ALPHA
    user32.UpdateLayeredWindow(hwnd, screen, None, ctypes.byref(wintypes.SIZE(w, h)), mem,
                               ctypes.byref(wintypes.POINT(0, 0)), 0, ctypes.byref(blend), 2)
    gdi32.SelectObject(mem, old)
    gdi32.DeleteObject(bmp)
    gdi32.DeleteDC(mem)
    user32.ReleaseDC(None, screen)


def work_area(hwnd):
    info = MONITORINFO(cbSize=ctypes.sizeof(MONITORINFO))
    user32.GetMonitorInfoW(user32.MonitorFromWindow(hwnd, 2), ctypes.byref(info))
    r = info.rcWork
    return r.left, r.top, r.right, r.bottom


def work_area_at(x, y):
    info = MONITORINFO(cbSize=ctypes.sizeof(MONITORINFO))
    user32.GetMonitorInfoW(user32.MonitorFromPoint(wintypes.POINT(x, y), 2), ctypes.byref(info))
    r = info.rcWork
    return r.left, r.top, r.right, r.bottom


_fonts = {}


def text_image(text, px, color, bold=False):
    """`text` drawn by Windows, so Arabic is joined and ordered right, as RGBA.

    Digits stay Western (0-9) whatever the locale, to match the ring's.
    """
    key = (px, bold)
    if key not in _fonts:
        _fonts[key] = gdi32.CreateFontW(-px, 0, 0, 0, 600 if bold else 400, 0, 0, 0,
                                        1, 0, 0, 4, 0, "Segoe UI")  # ANTIALIASED_QUALITY
    rtl = any("؀" <= ch <= "ۿ" for ch in text)
    flags = ETO_NUMERICSLATIN | (ETO_RTLREADING if rtl else 0)

    dc = gdi32.CreateCompatibleDC(None)
    old_font = gdi32.SelectObject(dc, _fonts[key])
    size = wintypes.SIZE()
    gdi32.GetTextExtentPoint32W(dc, text, len(text), ctypes.byref(size))
    w, h = int(size.cx * 1.4) + 4 * px, max(1, size.cy)  # room to spare, cropped below
    bmp, bits = dib(dc, w, h)
    old_bmp = gdi32.SelectObject(dc, bmp)
    gdi32.SetTextColor(dc, 0xFFFFFF)
    gdi32.SetBkMode(dc, 1)  # TRANSPARENT
    gdi32.ExtTextOutW(dc, px, 0, flags, None, text, len(text), None)
    gdi32.GdiFlush()
    raw = (ctypes.c_ubyte * (w * h * 4)).from_address(bits.value)
    cover = np.frombuffer(raw, np.uint8).reshape(h, w, 4)[..., 1].copy()
    gdi32.SelectObject(dc, old_bmp)
    gdi32.SelectObject(dc, old_font)
    gdi32.DeleteObject(bmp)
    gdi32.DeleteDC(dc)

    cols = np.flatnonzero(cover.max(axis=0))
    cover = cover[:, max(0, cols[0] - 1):cols[-1] + 2] if len(cols) else cover[:, :1]
    alpha = color[3] if len(color) == 4 else 255
    rgba = np.empty(cover.shape + (4,), np.uint8)
    rgba[..., :3] = color[:3]
    rgba[..., 3] = (cover.astype(np.uint16) * alpha // 255).astype(np.uint8)
    return Image.fromarray(rgba, "RGBA")


# --- the siren and the chime (live synthesis through waveOut) ---------------

class WAVEFORMATEX(ctypes.Structure):
    _fields_ = [("wFormatTag", wintypes.WORD), ("nChannels", wintypes.WORD),
                ("nSamplesPerSec", wintypes.DWORD), ("nAvgBytesPerSec", wintypes.DWORD),
                ("nBlockAlign", wintypes.WORD), ("wBitsPerSample", wintypes.WORD),
                ("cbSize", wintypes.WORD)]


class WAVEHDR(ctypes.Structure):
    _fields_ = [("lpData", ctypes.c_void_p), ("dwBufferLength", wintypes.DWORD),
                ("dwBytesRecorded", wintypes.DWORD), ("dwUser", ctypes.c_size_t),
                ("dwFlags", wintypes.DWORD), ("dwLoops", wintypes.DWORD),
                ("lpNext", ctypes.c_void_p), ("reserved", ctypes.c_size_t)]


winmm = ctypes.windll.winmm
winmm.waveOutOpen.argtypes = [ctypes.POINTER(wintypes.HANDLE), wintypes.UINT,
                              ctypes.POINTER(WAVEFORMATEX), ctypes.c_size_t, ctypes.c_size_t,
                              wintypes.DWORD]
for _name in ("waveOutPrepareHeader", "waveOutUnprepareHeader", "waveOutWrite"):
    getattr(winmm, _name).argtypes = [wintypes.HANDLE, ctypes.POINTER(WAVEHDR), wintypes.UINT]
winmm.waveOutReset.argtypes = [wintypes.HANDLE]
winmm.waveOutClose.argtypes = [wintypes.HANDLE]

WAVE_MAPPER, WHDR_DONE = 0xFFFFFFFF, 0x1


def mm(result):
    if result:
        raise OSError(f"waveOut error {result}")


def make_chime(rate):
    """Two soft bell strikes, the second higher: Claude is done."""
    t = np.arange(int(rate * 1.6)) / rate
    out = np.zeros(int(rate * 2.0))
    # The first strike waits a moment, for the siren to step back first.
    for start, hz in ((0.08, 987.8), (0.30, 1318.5)):
        bell = sum(a * np.sin(2 * np.pi * hz * m * t) * np.exp(-t * d)
                   for m, a, d in ((1, 1.0, 3.2), (2.0, 0.35, 5.0), (3.01, 0.18, 7.0), (4.2, 0.08, 9.0)))
        i = int(rate * start)
        out[i:i + len(t)] += bell * np.minimum(1, t / 0.004)
    return out / np.max(np.abs(out)) * 0.65


class Siren(threading.Thread):
    """A steady two-rotor siren whose pitch follows the percent left.

    Low while plenty is left, higher as it runs out; when the figure moves,
    the rotors spin up or down to the new pitch, so it glides, never jumps.
    The loudness stays the same throughout. A chime rings over it on request.
    """

    RATE, CHUNK, BUFFERS = 44100, 2048, 4
    LOW_HZ, HIGH_HZ = 150.0, 900.0  # pitch at 100% left and at 0% left
    CHORD = 1.2                     # second rotor, a minor third up
    GAIN = 0.95
    SPIN_UP, SPIN_DOWN = 1.2, 1.8   # seconds the rotors take to settle

    def __init__(self):
        super().__init__(daemon=True)
        self.left = None  # the percent the pitch follows; None is silence
        self.muted = False
        self.hz = None
        self.gain = 0.0
        self.duck = 1.0
        self.phases = [0.0, 0.0]
        self.samples = 0
        self.chime = make_chime(self.RATE)
        self.chime_at = None
        self.volume = self.vol_now = 1.0

    def ring(self):
        self.chime_at = 0

    def pitch(self, left):
        return self.LOW_HZ * (self.HIGH_HZ / self.LOW_HZ) ** ((100 - left) / 100)

    def siren(self, n, i):
        rate = self.RATE
        left, audible = self.left, self.left is not None and not self.muted

        if left is not None:
            target = self.pitch(left)
            if self.hz is None:
                self.hz = target
            tau = self.SPIN_UP if target > self.hz else self.SPIN_DOWN
            hz = target + (self.hz - target) * np.exp(-i / (tau * rate))
            self.hz = float(hz[-1])
        else:
            hz = np.full(n, self.hz or self.LOW_HZ)

        want = self.GAIN if audible else 0.0
        gain = want + (self.gain - want) * np.exp(-i / (0.25 * rate))  # soft on and off
        self.gain = float(gain[-1])
        if not audible and self.gain < 1e-4:
            self.gain = 0.0
            return np.zeros(n)

        t = (self.samples + i) / rate
        hz = hz * (1 + 0.004 * np.sin(2 * np.pi * 5.5 * t))  # the rotors' slight wobble
        x = np.zeros(n)
        for r, (ratio, level) in enumerate(((1.0, 1.0), (self.CHORD, 0.8))):
            f = hz * ratio
            phase = self.phases[r] + 2 * np.pi * np.cumsum(f) / rate
            self.phases[r] = float(phase[-1] % (2 * np.pi))
            for k in range(1, 11):
                # Each harmonic fades out smoothly as it nears 8 kHz.
                weight = np.clip((8000 - k * f) / 2000, 0, 1) / k ** 1.3
                x += level * weight * np.sin(k * phase)
        return np.tanh(2.0 * x / 3.5) * gain

    def render(self):
        n = self.CHUNK
        i = np.arange(1, n + 1)
        out = self.siren(n, i)
        self.samples += n

        at = self.chime_at
        want = 0.33 if at is not None else 1.0  # the siren steps back while the bell rings
        duck = want + (self.duck - want) * np.exp(-i / (0.05 * self.RATE))
        self.duck = float(duck[-1])
        out = out * duck
        if at is not None:
            part = self.chime[at:at + n]
            out[:len(part)] += part
            self.chime_at = at + n if at + n < len(self.chime) else None
        vol = self.volume + (self.vol_now - self.volume) * np.exp(-i / (0.05 * self.RATE))
        self.vol_now = float(vol[-1])
        return (np.clip(out * vol, -1, 1) * 32767).astype("<i2").tobytes()

    def run(self):
        while True:
            try:
                self.play()
            except Exception:
                log(traceback.format_exc())
            time.sleep(3)  # no device just now (unplugged, busy): try again

    def play(self):
        size = self.CHUNK * 2
        fmt = WAVEFORMATEX(1, 1, self.RATE, self.RATE * 2, 2, 16, 0)
        out = wintypes.HANDLE()
        mm(winmm.waveOutOpen(ctypes.byref(out), WAVE_MAPPER, ctypes.byref(fmt), 0, 0, 0))
        bufs, hdrs = [], []
        try:
            for _ in range(self.BUFFERS):
                buf = ctypes.create_string_buffer(size)
                hdr = WAVEHDR(lpData=ctypes.cast(buf, ctypes.c_void_p), dwBufferLength=size)
                mm(winmm.waveOutPrepareHeader(out, ctypes.byref(hdr), ctypes.sizeof(WAVEHDR)))
                bufs.append(buf)
                hdrs.append(hdr)
                self.queue(out, buf, hdr)
            while True:
                for buf, hdr in zip(bufs, hdrs):
                    while not hdr.dwFlags & WHDR_DONE:
                        time.sleep(0.005)
                    self.queue(out, buf, hdr)
        finally:
            winmm.waveOutReset(out)
            for hdr in hdrs:
                winmm.waveOutUnprepareHeader(out, ctypes.byref(hdr), ctypes.sizeof(WAVEHDR))
            winmm.waveOutClose(out)

    def queue(self, out, buf, hdr):
        data = self.render()
        ctypes.memmove(buf, data, len(data))
        hdr.dwFlags &= ~WHDR_DONE
        mm(winmm.waveOutWrite(out, ctypes.byref(hdr), ctypes.sizeof(WAVEHDR)))


# --- what to show -----------------------------------------------------------

def parse_time(iso):
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


def fmt_left(seconds):
    mins = max(1, int(-(-seconds // 60)))
    d, h, m = mins // 1440, (mins % 1440) // 60, mins % 60
    return f"{d}d {h}h" if d else f"{h}h {m}m" if h else f"{m}m"


def ar_count(n, one, two, few, many):
    """An Arabic count: يوم، يومان، 3 أيام، 11 يومًا."""
    return one if n == 1 else two if n == 2 else f"{n} {few}" if 3 <= n <= 10 else f"{n} {many}"


def span(seconds):
    mins = max(1, int(-(-seconds // 60)))
    if mins >= 1440:  # a week's window: days and hours
        d, h = mins // 1440, (mins % 1440) // 60
        if LANG[0] == "ar":
            days = ar_count(d, "يوم", "يومان", "أيام", "يومًا")
            return f"{days} و{ar_count(h, 'ساعة', 'ساعتان', 'ساعات', 'ساعة')}" if h else days
        return f"{d}d {h}h" if h else f"{d}d"
    h, m = mins // 60, mins % 60
    hu, mu = ("س", "د") if LANG[0] == "ar" else ("h", "m")
    return (f"{h}{hu} {m}{mu}" if m else f"{h}{hu}") if h else f"{m}{mu}"


def when(ts, now):
    """At what time, or on what day and time when it is not today."""
    if datetime.fromtimestamp(ts).date() == datetime.fromtimestamp(now).date():
        return T("at_time", t=clock(ts))
    return T("on_day", day=DAYS[LANG[0]][datetime.fromtimestamp(ts).weekday()], t=clock(ts))


def pct(n):
    """`n%` held left-to-right, so Arabic text around it never flips it to %n."""
    return f"‪{round(n)}%‬"


def clock(ts):
    d = datetime.fromtimestamp(ts)
    return f"{d.hour % 12 or 12}:{d.minute:02d}"


def color_for(left):
    if left is None:
        return GREY
    return RED if left <= 10 else AMBER if left <= 30 else GREEN


def read_json(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def window_view(win, now):
    """(percent left, reset text, reset timestamp) for one rate-limit window."""
    if not win:
        return None, "", None
    resets = parse_time(win.get("resetsAt") or "")
    if resets is not None and resets <= now:
        return 100.0, "reset", None
    left = max(0.0, min(100.0, 100.0 - float(win.get("percentUsed", 0))))
    return left, fmt_left(resets - now) if resets else "", resets


def forecast(history, left, resets, now, window=WINDOW):
    """(pace in percent per second, when the limit runs out at that pace or None).

    The pace is what was used over the last 45 minutes, measured from the last
    reading before then; short of one, from the earliest reading since, if the
    figure moved after it; short of that, the average since the window began
    (when nothing had been used).
    None until there are 5 minutes to go on.
    """
    used = 100 - left
    start = resets - window
    horizon = max(start, now - max(PACE, window / 10))
    readings = sorted((t, u) for t, u in history if start <= t <= now)
    before = [u for t, u in readings if t <= horizon]
    if window > 86400:
        t0, u0 = start, 0.0
    elif before:
        t0, u0 = horizon, before[-1]
    elif readings and readings[0][1] < used and now - readings[0][0] >= 5 * 60:
        t0, u0 = readings[0]  # it moved since the first reading: measure from there
    else:
        t0, u0 = start, 0.0
    if now - t0 < 5 * 60:
        return None
    pace = max(0.0, (used - u0) / (now - t0))
    return pace, (now + left / pace if pace > 0 else None)


class View:
    """Everything the ring and its card show, read once a second."""

    kind, window = "claude", WINDOW

    @property
    def title(self):
        return T("title")

    def __init__(self, usage, sessions, now):
        self.now = now
        limits = {w.get("kind"): w for w in (usage or {}).get("limits", [])}
        self.left, self.reset_text, self.resets = window_view(limits.get("five_hour"), now)
        self.weekly_left, _, self.weekly_resets = window_view(limits.get("seven_day"), now)
        self.history = [(t / 1000, u) for t, u in (usage or {}).get("history", [])]
        self.fc = (forecast(self.history, self.left, self.resets, now)
                   if self.left is not None and self.resets else None)

        live = [s for s in sessions if now - s.get("updatedAt", 0) / 1000 < STALE]
        working = [s for s in live if s.get("working")]
        self.working = bool(working)
        self.since = min((s.get("since", 0) / 1000 for s in working), default=now)
        # When the figure was last known good: the plugin's last write, a reply
        # finishing (the reading is checked then), or a turn running now.
        known = [(usage or {}).get("updatedAt", 0)]
        for s in sessions:
            known.append(s.get("doneAt", 0))
            if s.get("working"):
                known.append(s.get("updatedAt", 0))
        self.updated = max(known) / 1000 or None
        reset = self.left == 100 and self.resets is None  # a reset is known without a reading
        self.old = bool(self.updated and now - self.updated > OLD and not reset)

        recent = [s for s in sessions if s.get("context") is not None]
        self.context = max(recent, key=lambda s: s.get("updatedAt", 0))["context"] if recent else None

    @property
    def runs_out_early(self):
        return bool(self.fc and self.fc[1] and self.resets and self.fc[1] < self.resets)

    def ring_line(self):
        """The ring's second line: when the limit renews. In amber or red when, at this
        pace, it would run out first (the card says when)."""
        if self.reset_text == "reset":
            return T("ring_reset"), SOFT
        if not self.resets:
            return self.reset_text, None
        renews = datetime.fromtimestamp(self.resets)
        same_day = renews.date() == datetime.fromtimestamp(self.now).date()
        t = clock(self.resets) if same_day else DAYS[LANG[0]][renews.weekday()]
        color = SOFT
        if self.runs_out_early:
            color = RED if self.fc[1] - self.now < 30 * 60 else AMBER
        return T("ring_renew", t=t), color

    def forecast_line(self):
        if self.left is None or self.resets is None:
            return None, None
        if self.fc is None:
            return T("fc_wait"), DIM
        week = self.window > 86400
        pace, out_at = self.fc
        if self.runs_out_early:
            soon = out_at - self.now < 30 * 60
            early = span(self.resets - out_at)
            by = ("بـ" if early[:1].isdigit() else "ب") + early  # بـ3 أيام، بيومين
            by = by.replace("يومان", "يومين").replace("ساعتان", "ساعتين")  # after بـ the dual takes ـين
            line = T("fc_early_week" if week else "fc_early", when=when(out_at, self.now), span=early, by=by)
            return line, RED if soon else AMBER
        keep = max(0, round(self.left - pace * (self.resets - self.now)))
        return T("fc_ok_week" if week else "fc_ok", p=pct(keep)), GREEN

# --- Codex ------------------------------------------------------------------

def find_rate_limits(obj):
    if isinstance(obj, dict):
        if isinstance(obj.get("rate_limits"), dict):
            return obj["rate_limits"]
        obj = list(obj.values())
    if isinstance(obj, list):
        for item in obj:
            found = find_rate_limits(item)
            if found:
                return found
    return None


def by_number(paths):
    return sorted(paths, key=lambda p: int(p.name) if p.name.isdigit() else -1, reverse=True)


def codex_logs(count=4):
    """The newest few Codex session logs by name (rollout-<date>T<time>-<id>), newest first."""
    files = []
    try:
        for year in by_number(CODEX_LOGS.iterdir())[:2]:
            for month in by_number(year.iterdir())[:2]:
                for day in by_number(month.iterdir())[:3]:
                    files += day.glob("rollout-*.jsonl")
    except OSError:
        pass
    return sorted(files, key=lambda f: f.name, reverse=True)[:count]


def log_tail(path, size=512 * 1024):
    try:
        with path.open("rb") as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - size))
            return f.read().decode("utf-8", "ignore")
    except OSError:
        return ""


def reading_in(tail):
    """(rate_limits, when) from the last line of a log's tail that carries them, or None."""
    for line in reversed(tail.splitlines()):
        if '"rate_limits"' not in line:
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        found = find_rate_limits(obj)
        if found:
            when = parse_time(obj.get("timestamp") or "") if isinstance(obj, dict) else None
            return found, when or time.time()
    return None


TASK_STARTED = '"task_started"'
TASK_ENDED = ('"task_complete"', '"turn_aborted"', '"task_aborted"')
LAST_STAMP = re.compile(r'"timestamp":\s*"([^"]+)"')


class CodexWatch:
    """Codex's limits and whether it is at work, from what its session logs say."""

    def __init__(self):
        self.logs = {}  # path: {"size", "busy", "event"}
        self.reading, self.at = None, None
        self.candidates, self.next_scan = [], 0.0

    def poll(self, now):
        """Looks at the newest logs (cheaply: only those that grew are read); True on a new reading."""
        if now >= self.next_scan:
            self.next_scan = now + 5
            self.candidates = codex_logs()
        fresh = False
        for path in self.candidates:
            try:
                size = path.stat().st_size  # current even while Codex holds the file open
            except OSError:
                continue
            known = self.logs.get(path)
            if known and known["size"] == size:
                continue
            tail = log_tail(path)
            log = self.logs[path] = {"size": size, "busy": known["busy"] if known else False,
                                     "event": known["event"] if known else 0.0}
            started = tail.rfind(TASK_STARTED)
            ended = max(tail.rfind(mark) for mark in TASK_ENDED)
            if started >= 0 or ended >= 0:
                log["busy"] = started > ended  # a turn began and has not ended yet
            stamps = LAST_STAMP.findall(tail[-4096:]) or LAST_STAMP.findall(tail)
            if stamps:
                log["event"] = parse_time(stamps[-1]) or log["event"]
            found = reading_in(tail)
            if found and (self.at is None or found[1] > self.at):
                self.reading, self.at = found
                fresh = True
        return fresh

    def offer(self, limits, at):
        """A reading from elsewhere (a server); kept if it is the newest. True when kept."""
        if limits and at and (self.at is None or at > self.at):
            self.reading, self.at = limits, at
            return True
        return False

    def busy(self, now):
        """A turn is under way: started, not ended, and the log still moving within ten minutes."""
        return any(log["busy"] and now - log["event"] < 600 for log in self.logs.values())


# Codex on a server: the app runs it there over SSH, and it keeps its logs there.
# One SSH session per server runs this, read-only, and reports every 2 seconds.
REMOTE_WATCH = r"""
import glob, json, os, re, sys, time
from datetime import datetime
H = os.path.expanduser("~/.codex/sessions")
STAMP = re.compile(r'"timestamp":\s*"([^"]+)"')
ENDS = ('"task_complete"', '"turn_aborted"', '"task_aborted"')
def when(ts):
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
    except Exception:
        return None
def limits_in(o):
    if isinstance(o, dict):
        if isinstance(o.get("rate_limits"), dict):
            return o["rate_limits"]
        o = list(o.values())
    if isinstance(o, list):
        for i in o:
            r = limits_in(i)
            if r:
                return r
    return None
logs, best, best_at = {}, None, 0.0
while True:
    try:
        files = sorted(glob.glob(H + "/*/*/*/rollout-*.jsonl"), key=os.path.getmtime, reverse=True)[:4]
    except OSError:
        files = []
    for f in files:
        try:
            size = os.path.getsize(f)
        except OSError:
            continue
        log = logs.get(f)
        if log and log["size"] == size:
            continue
        with open(f, "rb") as fh:
            fh.seek(max(0, size - 512 * 1024))
            tail = fh.read().decode("utf-8", "ignore")
        log = logs[f] = {"size": size, "busy": log["busy"] if log else False, "event": log["event"] if log else 0.0}
        started, ended = tail.rfind('"task_started"'), max(tail.rfind(m) for m in ENDS)
        if started >= 0 or ended >= 0:
            log["busy"] = started > ended
        stamps = STAMP.findall(tail[-4096:]) or STAMP.findall(tail)
        if stamps:
            log["event"] = when(stamps[-1]) or log["event"]
        for line in reversed(tail.splitlines()):
            if '"rate_limits"' in line:
                try:
                    o = json.loads(line)
                except ValueError:
                    continue
                r = limits_in(o)
                if r:
                    at = when(o.get("timestamp") or "") or time.time()
                    if at > best_at:
                        best, best_at = r, at
                    break
    now = time.time()
    busy = any(l["busy"] and now - l["event"] < 600 for l in logs.values())
    print(json.dumps({"busy": busy, "limits": best, "at": best_at}), flush=True)
    time.sleep(2)
"""


class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [("PerProcessUserTimeLimit", ctypes.c_longlong), ("PerJobUserTimeLimit", ctypes.c_longlong),
                ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD)]


class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
                ("IoInfo", ctypes.c_ulonglong * 6), ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t), ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t)]


kernel32.CreateJobObjectW.restype = wintypes.HANDLE
kernel32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
_job = None


def tie_to_us(proc):
    """Puts a child in a job that Windows closes with this process: no ssh outlives the ring."""
    global _job
    if _job is None:
        _job = kernel32.CreateJobObjectW(None, None)
        info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        info.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        kernel32.SetInformationJobObject(_job, 9, ctypes.byref(info), ctypes.sizeof(info))
    kernel32.AssignProcessToJobObject(_job, int(proc._handle))


def codex_servers():
    """The SSH servers the Codex app connects to: (name, user@host, port, key file)."""
    try:
        state = json.loads(CODEX_STATE.read_text(encoding="utf-8"))
    except Exception:
        return []

    def find(obj):
        if isinstance(obj, dict):
            if isinstance(obj.get("codex-managed-remote-connections"), list):
                return obj["codex-managed-remote-connections"]
            obj = list(obj.values())
        if isinstance(obj, list):
            for item in obj:
                found = find(item)
                if found:
                    return found
        return None

    servers = []
    for c in find(state) or []:
        if isinstance(c, dict) and c.get("hostname"):
            servers.append((c.get("displayName") or c["hostname"], c["hostname"], c.get("sshPort"), c.get("identity")))
    return servers


class RemoteCodex(threading.Thread):
    """One server's Codex, as the watcher running there reports it."""

    def __init__(self, name, host, port, key):
        super().__init__(daemon=True)
        self.name, self.host, self.port, self.key = name, host, port, key
        self.state, self.seen = None, 0.0
        self.proc, self.stopped = None, False

    def run(self):
        ssh = shutil.which("ssh") or r"C:\Windows\System32\OpenSSH\ssh.exe"
        while not self.stopped:
            argv = [ssh, "-T", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", "-o", "ServerAliveInterval=15"]
            if self.key:
                argv += ["-i", self.key]
            if self.port:
                argv += ["-p", str(self.port)]
            argv += [self.host, "python3 -u -"]
            try:
                self.proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                             stderr=subprocess.DEVNULL, creationflags=0x08000000)  # no window
                tie_to_us(self.proc)
                self.proc.stdin.write(REMOTE_WATCH.encode())
                self.proc.stdin.close()  # the script is all there is: now it runs
                for line in self.proc.stdout:
                    try:
                        self.state = json.loads(line)
                        self.seen = time.time()
                    except ValueError:
                        pass
            except Exception:
                log(traceback.format_exc())
            finally:
                if self.proc and self.proc.poll() is None:
                    self.proc.kill()
            for _ in range(30):  # cut off (sleep, no network): try again in a while
                if self.stopped:
                    return
                time.sleep(0.5)

    def busy(self, now):
        return bool(self.state and self.state.get("busy") and now - self.seen < 10)

    def stop(self):
        self.stopped = True
        if self.proc and self.proc.poll() is None:
            self.proc.kill()


def codex_windows(limits):
    """Its windows, the shortest first (the session's, then the week's)."""
    found = [w for w in (limits.get("primary"), limits.get("secondary")) if isinstance(w, dict)]
    return sorted(found, key=lambda w: w.get("window_minutes") or 0)


def codex_limit(w):
    if not w:
        return None
    at = w.get("resets_at")
    iso = datetime.fromtimestamp(at, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if at else ""
    return {"percentUsed": w.get("used_percent", 0), "resetsAt": iso}


class CodexView(View):
    """What the Codex ring and its card show: one View, fed from Codex's logs."""

    kind = "codex"

    @property
    def title(self):
        return "Codex"

    def __init__(self, limits, at, history, now, working=False):
        self.now = now
        wins = codex_windows(limits) + [None, None]
        main, other = wins[0], wins[1]
        self.window = (main or {}).get("window_minutes", 300) * 60
        self.left, self.reset_text, self.resets = window_view(codex_limit(main), now)
        self.weekly_left, _, self.weekly_resets = window_view(codex_limit(other), now)
        self.history = history
        self.fc = (forecast(history, self.left, self.resets, now, self.window)
                   if self.left is not None and self.resets else None)
        self.working, self.since, self.context = working, now, None
        self.updated = at
        reset = self.left == 100 and self.resets is None
        self.old = bool(at and now - at > max(OLD, self.window / 24) and not reset)  # read when Codex runs
        self.plan = limits.get("plan_type") or ""


def axis_label(ts, window):
    if window > 86400:  # a week: the day as well
        return f"{DAYS[LANG[0]][datetime.fromtimestamp(ts).weekday()]} {clock(ts)}"
    return clock(ts)


# --- drawing ----------------------------------------------------------------

DISC = (28, 28, 31)


def over(rgb, alpha):
    """`rgb` at `alpha` over the disc, as one opaque color."""
    return tuple(round(d + (c - d) * alpha / 255) for c, d in zip(rgb, DISC)) + (255,)


def load_font(size, bold):
    names = ["seguisb.ttf", "segoeuib.ttf", "arialbd.ttf"] if bold else ["segoeui.ttf", "arial.ttf"]
    for name in names:
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()


def speaker_icon(px, color, muted):
    """A speaker: with a cross when the siren is muted, with sound waves when not."""
    S = px * 4
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    ink = color + (255,)
    d.rectangle([0.10 * S, 0.37 * S, 0.30 * S, 0.63 * S], fill=ink)
    d.polygon([(0.28 * S, 0.37 * S), (0.54 * S, 0.14 * S), (0.54 * S, 0.86 * S), (0.28 * S, 0.63 * S)], fill=ink)
    w = max(2, int(0.09 * S))
    if muted:
        d.line([(0.66 * S, 0.36 * S), (0.92 * S, 0.64 * S)], fill=ink, width=w)
        d.line([(0.66 * S, 0.64 * S), (0.92 * S, 0.36 * S)], fill=ink, width=w)
    else:
        for r in (0.16, 0.30):
            box = [0.54 * S - r * S, 0.5 * S - r * S, 0.54 * S + r * S, 0.5 * S + r * S]
            d.arc(box, -50, 50, fill=ink, width=w)
    return img.resize((px, px), Image.LANCZOS)


class Ring:
    """Draws the ring: a still base per state, plus cached glow and spinner layers."""

    def __init__(self, size, accent=None, tag=None, comet=CLAUDE):
        self.size = size
        self.accent, self.tag, self.comet_color = accent, tag, comet
        self.scale = size / BASE
        self.k = self.scale * SS
        k = self.k
        self.f_pct = load_font(int(26 * k), True)
        self.f_small = load_font(int(10.5 * k), False)
        self.f_tiny = load_font(int(9 * k), True)
        self.f_tag = load_font(int(8.5 * k), True)
        self.m, self.inset, self.rw = 9 * k, 15 * k, int(7 * k)
        self.glows = {}
        self.spinner = {}

    def base(self, left, line, line_color, weekly_left, muted):
        k, S = self.k, self.size * SS
        m, inset, rw = self.m, self.inset, self.rw
        color = color_for(left)
        if self.accent and color == GREEN:
            color = self.accent
        img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
        ImageDraw.Draw(img).ellipse([m, m, S - m, S - m], fill=DISC + (242,),
                                    outline=(72, 72, 76, 242), width=max(1, int(1.2 * k)))
        d = ImageDraw.Draw(img)
        box = [inset, inset, S - inset, S - inset]
        d.arc(box, 0, 360, fill=over(WHITE, 30), width=rw)
        if left is not None and left > 0:
            end = -90 + 360 * left / 100
            d.arc(box, -90, end, fill=color + (255,), width=rw)
            r = (S - 2 * inset) / 2 - rw / 2
            for ang in (-90, end):  # round caps
                x = S / 2 + r * math.cos(math.radians(ang))
                y = S / 2 + r * math.sin(math.radians(ang))
                d.ellipse([x - rw / 2, y - rw / 2, x + rw / 2, y + rw / 2], fill=color + (255,))

        c = S / 2
        pct = "--" if left is None else f"{int(round(left))}%"
        d.text((c, c - 6 * k), pct, font=self.f_pct, fill=over(WHITE, 245), anchor="mm")
        if self.tag:
            d.text((c, c - 27 * k), self.tag, font=self.f_tag, fill=over(WHITE, 150), anchor="mm")
        if line and line_color is None:
            d.text((c, c + 14 * k), line, font=self.f_small, fill=over(WHITE, 150), anchor="mm")
        if weekly_left is not None and not muted:
            wcol = color_for(weekly_left) if weekly_left <= 30 else WHITE
            d.text((c, c + 27 * k), f"W {int(round(weekly_left))}%", font=self.f_tiny,
                   fill=over(wcol, 120 if weekly_left > 30 else 230), anchor="mm")
        img = img.resize((self.size, self.size), Image.LANCZOS)

        # Arabic lines are Windows text, laid over the finished ring.
        s, half = self.scale, self.size / 2
        if line and line_color is not None:
            t = text_image(line, round(10.5 * s), over(line_color, 255), bold=True)
            img.alpha_composite(t, (int(half - t.width / 2), int(half + 14 * s - t.height / 2)))
        if muted:
            icon = speaker_icon(round(12 * s), over(AMBER, 255)[:3], True)
            img.alpha_composite(icon, (int(half - icon.width / 2), int(half + 27 * s - icon.height / 2)))
        return img

    def glow(self, color):
        if color not in self.glows:
            k, S, m = self.k, self.size * SS, self.m
            layer = Image.new("RGBA", (S, S), (0, 0, 0, 0))
            ImageDraw.Draw(layer).ellipse([m + k, m + k, S - m - k, S - m - k],
                                          outline=color + (255,), width=int(6 * k))
            layer = layer.filter(ImageFilter.GaussianBlur(3.5 * k))
            self.glows[color] = np.asarray(layer.resize((self.size, self.size), Image.LANCZOS)).copy()
        return self.glows[color]

    def comet(self, frame):
        """A short orange comet circling inside the disc's rim: Claude is working."""
        k, S = self.k, self.size * SS
        layer = Image.new("RGBA", (S, S), (0, 0, 0, 0))
        d = ImageDraw.Draw(layer)
        r = S / 2 - self.m - 3 * k
        head = -90 + 360 * frame / FRAMES
        steps = 34
        for j in reversed(range(steps)):
            ang = math.radians(head - j * 3.0)
            fade = (1 - j / steps) ** 1.3
            rad = 2.9 * k * (1 - j / (steps * 1.5))
            x, y = S / 2 + r * math.cos(ang), S / 2 + r * math.sin(ang)
            d.ellipse([x - rad, y - rad, x + rad, y + rad], fill=self.comet_color + (int(255 * fade),))
        return layer.resize((self.size, self.size), Image.LANCZOS)

    def compose(self, base, glow_color, glow, frame, faded=False):
        img = Image.new("RGBA", base.size, (0, 0, 0, 0))
        if glow_color and glow > 0:
            g = self.glow(glow_color).copy()
            g[..., 3] = (g[..., 3] * glow).astype(np.uint8)
            img = Image.fromarray(g, "RGBA")
        img.alpha_composite(base)
        if frame is not None:
            if frame not in self.spinner:
                self.spinner[frame] = self.comet(frame)
            img.alpha_composite(self.spinner[frame])
        if faded:  # an out-of-date figure: the whole ring goes pale
            a = np.asarray(img).copy()
            a[..., 3] = (a[..., 3] * 0.5).astype(np.uint8)
            img = Image.fromarray(a, "RGBA")
        return img


class Card:
    """The hover card: what is left, a graph with the forecast, weekly, context, Claude."""

    def __init__(self, scale):
        self.k = scale

    def text(self, s, px, color, bold=False, max_w=None):
        px = round(px * self.k)
        img = text_image(s, px, color, bold)
        while max_w and img.width > max_w and px > 8:
            px -= 1
            img = text_image(s, px, color, bold)
        return img

    def wrapped(self, s, px, color, bold, max_w):
        """One line when it fits, else two, broken at the comma, so it never shrinks to unreadable."""
        if text_image(s, round(px * self.k), color, bold).width <= max_w:
            return [text_image(s, round(px * self.k), color, bold)]
        for sep in ("، ", ", "):
            if sep in s:
                head, tail = s.split(sep, 1)
                return [self.text(head + sep.strip(), px, color, bold, max_w), self.text(tail, px, color, bold, max_w)]
        return [self.text(s, px, color, bold, max_w)]

    def render(self, v, muted, volume=1.0):
        k = self.k
        rtl = LANG[0] == "ar"
        W, pad = int(330 * k), int(16 * k)
        inner = W - 2 * pad
        items = []
        at_right = lambda img, y: items.append((W - pad - img.width, y, img))
        at_left = lambda img, y: items.append((pad, y, img))
        label, value = (at_right, at_left) if rtl else (at_left, at_right)
        y = pad

        title = self.text(v.title, 15, WHITE, True)
        label(title, y)
        if v.left is None:
            note = self.text(T("no_reading"), 12.5, DIM)
            value(note, y + title.height - note.height)
        else:
            num = self.text(f"{round(v.left)}%", 17, color_for(v.left), True)
            word = self.text(T("left_word"), 12, SOFT)
            gap = int(4 * k)
            x = pad if rtl else W - pad - num.width - gap - word.width
            base_y = y + title.height
            items.append((x, base_y - num.height + int(2 * k), num))
            items.append((x + num.width + gap, base_y - word.height, word))
        y += title.height + int(3 * k)

        sub = None
        if v.resets:
            if v.window > 86400:
                day = DAYS[LANG[0]][datetime.fromtimestamp(v.resets).weekday()]
                sub = T("resets_on", day=day, t=clock(v.resets), span=span(v.resets - v.now))
            else:
                sub = T("resets_at", t=clock(v.resets), span=span(v.resets - v.now))
        elif v.left == 100:
            sub = T("reset_done")
        if sub:
            for s in self.wrapped(sub, 12, SOFT, False, inner):
                label(s, y)
                y += s.height
        y += int(10 * k)

        gh = int(92 * k)
        items.append((pad, y, self.graph(v, inner, gh)))
        y += gh + int(3 * k)
        if v.resets:  # time runs left to right in either language
            a = self.text(axis_label(v.resets - v.window, v.window), 10.5, DIM)
            b = self.text(axis_label(v.resets, v.window), 10.5, DIM)
            at_left(a, y)
            at_right(b, y)
            y += a.height
        y += int(8 * k)

        line, color = v.forecast_line()
        if line:
            for f in self.wrapped(line, 12.5, color, True, inner):
                label(f, y)
                y += f.height
            y += int(12 * k)

        divider = y
        y += int(12 * k)

        def row(name, val, color, note=None):
            nonlocal y
            lab = self.text(name, 12.5, SOFT)
            out = self.text(val, 12.5, color, True)
            label(lab, y)
            value(out, y)
            y += max(lab.height, out.height)
            if note:
                n = self.text(note, 10.5, DIM, max_w=inner)
                label(n, y)
                y += n.height
            y += int(8 * k)

        if v.weekly_left is not None:
            note = None
            if v.weekly_resets:
                d = datetime.fromtimestamp(v.weekly_resets)
                note = T("weekly_resets", day=DAYS[LANG[0]][d.weekday()], t=clock(v.weekly_resets))
            wl = v.weekly_left
            row(T("weekly"), T("weekly_left", p=pct(wl)), color_for(wl) if wl <= 30 else WHITE, note)
        if v.context is not None:
            c = v.context
            row(T("context"), T("context_used", p=pct(c)), RED if c >= 90 else AMBER if c >= 75 else WHITE)
        if v.kind == "claude":
            if v.working:
                mins, secs = divmod(int(v.now - v.since), 60)
                row("Claude", T("working", m=mins, s=f"{secs:02d}"), CLAUDE)
            else:
                row("Claude", T("ready"), GREEN)
            icon = speaker_icon(round(16 * k), AMBER if muted else SOFT, muted)
            label(icon, y)
            if not muted:
                val = self.text(pct(volume * 100), 12.5, WHITE, True)
                value(val, y + (icon.height - val.height) // 2)
            y += icon.height + int(8 * k)
        else:
            where = getattr(v, "where", None)
            state = T("busy_on", where=where) if v.working and where else T("busy") if v.working else T("ready")
            row("Codex", state, CODEX_COMET if v.working else GREEN)
            if v.plan:
                row(T("plan"), v.plan, WHITE)
        if v.updated:
            ago = v.now - v.updated
            row(T("updated"), T("just_now") if ago < 60 else T("ago", span=span(ago)),
                AMBER if v.old else SOFT,
                T("old_note_codex" if v.kind == "codex" else "old_note") if v.old else None)

        H = y - int(8 * k) + pad
        bg = Image.new("RGBA", (W * 2, H * 2), (0, 0, 0, 0))
        ImageDraw.Draw(bg).rounded_rectangle([1, 1, W * 2 - 2, H * 2 - 2], radius=int(28 * k),
                                             fill=(24, 24, 27, 248), outline=(70, 70, 74, 255),
                                             width=max(2, int(2 * k)))
        img = bg.resize((W, H), Image.LANCZOS)
        ImageDraw.Draw(img).line([(pad, divider), (W - pad, divider)], fill=(255, 255, 255, 22), width=1)
        for x, yy, piece in items:
            img.alpha_composite(piece, (int(x), int(yy)))
        return img

    def graph(self, v, w, h):
        """% left across the 5-hour window, with the pace carried on as a dashed line."""
        S2 = 2
        k = self.k * S2
        W2, H2 = w * S2, h * S2
        img = Image.new("RGBA", (W2, H2), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        top, bottom = 4 * k, H2 - 4 * k
        Y = lambda pct: top + (1 - pct / 100) * (bottom - top)
        for pct in (100, 50, 0):
            d.line([(0, Y(pct)), (W2, Y(pct))], fill=(255, 255, 255, 30 if pct != 50 else 18), width=S2)
        if v.left is None or v.resets is None:
            return img.resize((w, h), Image.LANCZOS)

        start = v.resets - v.window
        X = lambda t: (min(max(t, start), v.resets) - start) / v.window * (W2 - 1)
        # Straight up to the first reading (when it was spent is unknown), then
        # each reading holds until the next one.
        pts = [(start, 100.0)]
        for t, used in sorted((t, u) for t, u in v.history if start <= t < v.now):
            if len(pts) > 1:
                pts.append((t, pts[-1][1]))
            pts.append((t, 100 - used))
        if len(pts) > 1:
            pts.append((v.now, pts[-1][1]))
        pts.append((v.now, v.left))
        line = [(X(t), Y(p)) for t, p in pts]
        color = color_for(v.left)

        area = Image.new("RGBA", (W2, H2), (0, 0, 0, 0))
        ImageDraw.Draw(area).polygon(line + [(X(v.now), Y(0)), (X(start), Y(0))], fill=color + (40,))
        img.alpha_composite(area)
        d = ImageDraw.Draw(img)
        d.line(line, fill=color + (255,), width=int(2.2 * k), joint="curve")

        if v.fc:
            pace, out_at = v.fc
            end_t = min(out_at, v.resets) if out_at else v.resets
            end_left = max(0.0, v.left - pace * (end_t - v.now))
            dash_color = (RED if v.runs_out_early else color) + (230,)
            x0, y0, x1, y1 = X(v.now), Y(v.left), X(end_t), Y(end_left)
            length = math.hypot(x1 - x0, y1 - y0)
            on, off = 6 * k, 4 * k
            pos = 0.0
            while pos < length:
                a, b = pos / length, min(pos + on, length) / length
                d.line([(x0 + (x1 - x0) * a, y0 + (y1 - y0) * a), (x0 + (x1 - x0) * b, y0 + (y1 - y0) * b)],
                       fill=dash_color, width=int(1.8 * k))
                pos += on + off
            if v.runs_out_early:
                r = 3.5 * k
                d.ellipse([x1 - r, y1 - r, x1 + r, y1 + r], fill=RED + (255,))

        d.line([(X(v.now), top), (X(v.now), bottom)], fill=(255, 255, 255, 50), width=S2)
        r = 3.5 * k
        d.ellipse([X(v.now) - r, Y(v.left) - r, X(v.now) + r, Y(v.left) + r], fill=WHITE + (255,))
        return img.resize((w, h), Image.LANCZOS)


# --- the icon by the clock, and starting with Windows -----------------------

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_NAME = "ClaudeSessionLimit"


def set_autostart(on):
    """Adds or removes the ring from what Windows starts at sign-in (this user only)."""
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
        if on:
            winreg.SetValueEx(key, RUN_NAME, 0, winreg.REG_SZ, f'"{pythonw}" "{Path(__file__).resolve()}"')
        else:
            try:
                winreg.DeleteValue(key, RUN_NAME)
            except FileNotFoundError:
                pass


def tray_image(left, faded, size, accent=None, disc=False):
    """A badge in the limit's color with the percent left on it: a rounded
    square for Claude, a disc in its own accent for Codex."""
    S = size * 4
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    color = color_for(left)
    if accent and color == GREEN:
        color = accent
    if disc:
        d.ellipse([0, 0, S - 1, S - 1], fill=color + (255,))
    else:
        d.rounded_rectangle([0, 0, S - 1, S - 1], radius=int(S * 0.28), fill=color + (255,))
    text = "-" if left is None else str(round(left))
    size_for = {1: 0.78, 2: 0.68}.get(len(text), 0.5) * (0.9 if disc else 1)
    ink = (255, 255, 255) if left is not None and left <= 10 else (18, 18, 20)
    d.text((S / 2, S / 2 + S * 0.03), text, font=load_font(int(S * size_for), True), fill=ink + (255,),
           anchor="mm")
    img = img.resize((size, size), Image.LANCZOS)
    if faded:
        a = np.asarray(img).copy()
        a[..., 3] = (a[..., 3] * 0.55).astype(np.uint8)
        img = Image.fromarray(a, "RGBA")
    return img


def make_hicon(img):
    """A Windows icon from an RGBA image, alpha kept."""
    w, h = img.size
    data = np.asarray(img, dtype=np.uint8)[..., [2, 1, 0, 3]].tobytes()
    screen = user32.GetDC(None)
    color, bits = dib(screen, w, h)
    ctypes.memmove(bits, data, len(data))
    mask = gdi32.CreateBitmap(w, h, 1, 1, (ctypes.c_ubyte * (((w + 15) // 16) * 2 * h))())
    icon = user32.CreateIconIndirect(ctypes.byref(ICONINFO(True, 0, 0, mask, color)))
    gdi32.DeleteObject(color)
    gdi32.DeleteObject(mask)
    user32.ReleaseDC(None, screen)
    return icon


class Tray:
    """The icon by the clock: hover shows the card, a click shows or hides the
    ring, a right-click opens the menu."""

    ID, MSG = 1, WM_APP + 1

    def __init__(self, widget, cls="ClaudeSessionLimitTray", accent=None, disc=False, visible=True):
        self.w = widget
        self.accent, self.disc = accent, disc
        self.taskbar_created = user32.RegisterWindowMessageW("TaskbarCreated")
        self.proc = WNDPROC(self.wndproc)  # held, or Windows calls freed memory
        inst = kernel32.GetModuleHandleW(None)
        # A class of its own per icon: each window's messages go to its own Tray.
        user32.RegisterClassW(ctypes.byref(WNDCLASSW(lpfnWndProc=self.proc, hInstance=inst,
                                                     lpszClassName=cls)))
        self.hwnd = user32.CreateWindowExW(0, cls, cls, 0, 0, 0, 0, 0, None, None, inst, None)
        self.size = user32.GetSystemMetrics(49) or 16  # SM_CXSMICON
        self.icon = make_hicon(tray_image(None, False, self.size, accent, disc))
        self.tip, self.key = "", None
        self.point = (0, 0, 0, 0)
        # What the icon was asked, for the ring to act on (Tk must not be
        # called from inside a window message: Python dies of it).
        self.events = collections.deque()
        self.added = self.add() if visible else False  # Codex's waits for its first reading

    def data(self, flags):
        nid = NOTIFYICONDATAW(cbSize=ctypes.sizeof(NOTIFYICONDATAW), hWnd=self.hwnd, uID=self.ID,
                              uFlags=flags, uCallbackMessage=self.MSG, hIcon=self.icon)
        nid.szTip = self.tip[:127]
        return nid

    def add(self):
        nid = self.data(NIF_MESSAGE | NIF_ICON | NIF_TIP)
        added = bool(shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(nid)))
        nid.uVersion = 4  # hover reaches us as NIN_POPUPOPEN, so the card can show
        shell32.Shell_NotifyIconW(NIM_SETVERSION, ctypes.byref(nid))
        return added

    def update(self, left, faded, tip):
        key = (None if left is None else round(left), faded, tip)
        if key == self.key:
            return
        self.key = key
        old, self.icon = self.icon, make_hicon(tray_image(left, faded, self.size, self.accent, self.disc))
        self.tip = tip
        if self.added:
            shell32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(self.data(NIF_ICON | NIF_TIP)))
        else:
            self.added = self.add()
        user32.DestroyIcon(old)

    def remove(self):
        if self.added:
            shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(self.data(0)))

    def rect(self):
        """Where the pointer was when it came onto the icon. (The icon's own rect
        comes back mirrored on a right-to-left taskbar, and is the overflow
        arrow's while the icon hides there; the pointer is right on both.)"""
        return self.point

    def mark(self):
        p = wintypes.POINT()
        user32.GetCursorPos(ctypes.byref(p))
        self.point = (p.x, p.y, p.x, p.y)

    def wndproc(self, hwnd, msg, wparam, lparam):
        try:
            if msg == self.MSG:
                event = lparam & 0xFFFF
                if event in (NIN_SELECT, NIN_KEYSELECT):
                    self.events.append("click")
                elif event == WM_CONTEXTMENU:
                    self.events.append("menu")
                elif event == NIN_POPUPOPEN:
                    self.mark()
                    self.events.append("hover")
                elif event == NIN_POPUPCLOSE:
                    self.events.append("leave")
                return 0
            if msg == WM_SHOW_RING:
                self.events.append("show")
                return 0
            if msg == self.taskbar_created:  # Explorer restarted: put the icon back
                self.key = None
                if self.added:
                    self.add()
                return 0
        except Exception:
            log(traceback.format_exc())
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)


def grip_image(r):
    """The resize grip: a small dark disc with a diagonal two-way arrow."""
    S = r * 2 * 4
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse([2, 2, S - 3, S - 3], fill=(44, 44, 48, 240), outline=(120, 120, 126, 255), width=max(2, S // 20))
    c, a, w = S / 2, S * 0.2, max(3, int(S * 0.08))
    ink = (235, 235, 240, 255)
    d.line([(c - a, c - a), (c + a, c + a)], fill=ink, width=w)
    for x, y, sx in ((c - a, c - a, 1), (c + a, c + a, -1)):  # the two arrowheads
        d.line([(x, y), (x + sx * a * 0.8, y)], fill=ink, width=w)
        d.line([(x, y), (x, y + sx * a * 0.8)], fill=ink, width=w)
    return img.resize((r * 2, r * 2), Image.LANCZOS)


# --- the widget -------------------------------------------------------------

class Widget:
    def __init__(self):
        self.state = self.load_state()
        LANG[0] = self.state.get("lang", "ar")
        self.root = tk.Tk()
        self.root.withdraw()
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)

        self.scale = user32.GetDpiForSystem() / 96 if hasattr(user32, "GetDpiForSystem") else 1
        self.min_size, self.max_size = int(70 * self.scale), int(420 * self.scale)
        self.size = self.clamp_size(int(BASE * self.scale * self.state.get("ring_scale", 1.0)))
        self.ring = Ring(self.size, tag="Claude")
        self.grip_r = round(8 * self.scale)
        self.grip = grip_image(self.grip_r)
        self.resizing = None   # (size, pointer x, pointer y) when the drag began
        self.last_img = None
        self.card_painter = Card(self.scale)
        x, y = self.initial_position()
        self.root.geometry(f"{self.size}x{self.size}+{x}+{y}")
        self.root.deiconify()
        self.root.update_idletasks()
        wid = self.root.winfo_id()
        self.hwnd = user32.GetParent(wid) or wid
        make_layered(self.hwnd)

        # The card: a second layered window, click-through, shown on hover.
        self.card = tk.Toplevel(self.root)
        self.card.overrideredirect(True)
        self.card.attributes("-topmost", True)
        self.card.geometry("1x1+-10000+-10000")
        self.card.update_idletasks()
        cwid = self.card.winfo_id()
        self.card_hwnd = user32.GetParent(cwid) or cwid
        make_layered(self.card_hwnd, WS_EX_TRANSPARENT | WS_EX_NOACTIVATE)
        blit(self.card_hwnd, Image.new("RGBA", (1, 1), (0, 0, 0, 0)))
        self.card.withdraw()
        self.card_shown = False
        self.card_next = 0.0
        self.hover_job = None

        self.drag = None
        self.preview = None   # (percent, until): a pitch to hear for a moment
        self.flash_until = 0.0
        self.usage, self.mtime = {}, None
        self.sessions = []
        self.done_seen = {}
        self.next_read = self.next_prune = 0.0
        self.view = None
        self.base_key = self.frame_key = None
        self.base = None

        self.siren = Siren()
        self.siren.muted = self.state.get("muted", False)
        self.siren.volume = self.siren.vol_now = self.state.get("volume", 1.0)
        self.vol_until = 0.0  # the ring shows the volume until then
        self.refresh_asked = 0.0  # when "refresh now" was pressed, until a reading comes
        self.refresh_note = None  # (text, color, until): how the refresh went

        self.chime_on = tk.BooleanVar(value=self.state.get("chime", True))
        self.volume_var = tk.IntVar(value=round(self.siren.volume * 100))
        self.ring_var = tk.BooleanVar(value=not self.state.get("ring_hidden", False))
        self.autostart_var = tk.BooleanVar(value=self.state.get("autostart", True))
        self.lang_var = tk.StringVar(value=LANG[0])
        self.menu = tk.Menu(self.root, tearoff=0)
        self.submenus = []

        r = self.root
        r.bind("<ButtonPress-1>", self.on_press)
        r.bind("<B1-Motion>", self.on_drag)
        r.bind("<ButtonRelease-1>", self.on_release)
        r.bind("<Double-Button-1>", self.on_double)
        r.bind("<Motion>", self.on_motion)
        r.bind("<Button-3>", self.on_menu)
        r.bind("<Enter>", self.on_enter)
        r.bind("<Leave>", lambda e: self.hide_card())
        r.bind("<MouseWheel>", self.on_wheel)
        r.bind("<Button-2>", lambda e: self.refresh_now())  # the wheel pressed

        self.card_anchor = "ring"
        self.tray = Tray(self)
        self.poll_tray()
        self.make_codex()
        self.toggle_autostart()
        if self.state.get("ring_hidden"):
            if SHOW:
                self.show_ring()
            else:
                self.root.withdraw()

        self.siren.start()
        self.tick()

    # state ---------------------------------------------------------------

    def load_state(self):
        return read_json(STATE) or {}

    def save_state(self):
        try:
            STATE.write_text(json.dumps(self.state), encoding="utf-8")
        except OSError as e:
            log(f"save_state: {e}")

    def initial_position(self):
        vx, vy = user32.GetSystemMetrics(76), user32.GetSystemMetrics(77)
        vw, vh = user32.GetSystemMetrics(78), user32.GetSystemMetrics(79)
        x, y = self.state.get("x"), self.state.get("y")
        if x is None or y is None or not (vx <= x <= vx + vw - 20 and vy <= y <= vy + vh - 20):
            x = self.root.winfo_screenwidth() - self.size - int(24 * self.scale)
            y = self.root.winfo_screenheight() - self.size - int(72 * self.scale)
        return x, y

    # menu ----------------------------------------------------------------

    def build_menu(self):
        m = self.menu
        m.delete(0, "end")
        for sub in self.submenus:
            sub.destroy()
        self.submenus = [tk.Menu(m, tearoff=0) for _ in range(3)]
        vol, test, lang = self.submenus

        m.add_command(label=T("refresh_now"), command=self.refresh_now)
        m.add_separator()
        muted = self.state.get("muted", False)
        m.add_command(label=T("unmute") if muted else T("mute"), command=self.toggle_mute)
        self.volume_var.set(round(self.siren.volume * 100))
        for p in (10, 25, 50, 75, 100):
            vol.add_radiobutton(label=f"{p}%", value=p, variable=self.volume_var, command=self.set_volume)
        m.add_cascade(label=T("volume"), menu=vol)
        m.add_checkbutton(label=T("chime"), variable=self.chime_on, command=self.toggle_chime)
        for p in (100, 50, 30, 10, 0):
            test.add_command(label=T("siren_at", p=p), command=lambda p=p: self.hear(p))
        test.add_separator()
        test.add_command(label=T("chime_test"), command=self.siren.ring)
        m.add_cascade(label=T("preview"), menu=test)
        m.add_separator()
        m.add_checkbutton(label=T("show_ring"), variable=self.ring_var, command=self.toggle_ring)
        m.add_checkbutton(label=T("startup"), variable=self.autostart_var, command=self.toggle_autostart)
        if getattr(self, "codex_remote_var", None) is not None:
            m.add_checkbutton(label=T("codex_remote"), variable=self.codex_remote_var, command=self.toggle_remotes)
        lang.add_radiobutton(label="العربية", value="ar", variable=self.lang_var, command=self.set_lang)
        lang.add_radiobutton(label="English", value="en", variable=self.lang_var, command=self.set_lang)
        m.add_cascade(label="اللغة / Language", menu=lang)
        m.add_separator()
        m.add_command(label=T("quit"), command=self.root.destroy)

    def refresh_now(self):
        """Asks an open Claude Code session for a fresh reading (the plugin's
        answerRefresh makes the smallest model call and writes what comes back)."""
        now = time.time()
        try:
            (DATA / "refresh.json").write_text(json.dumps({"askedAt": int(now * 1000)}), encoding="utf-8")
        except OSError as e:
            log(f"refresh: {e}")
            return
        self.refresh_asked, self.refresh_note = now, None
        self.next_read = 0.0

    def set_volume(self, level=None):
        level = self.volume_var.get() / 100 if level is None else level
        self.siren.volume = self.state["volume"] = round(min(1.0, max(0.0, level)), 2)
        self.vol_until = time.time() + 1.5
        self.save_state()

    def on_wheel(self, e):
        self.set_volume(self.siren.volume + (0.05 if e.delta > 0 else -0.05))

    def toggle_ring(self):
        self.state["ring_hidden"] = not self.ring_var.get()
        self.save_state()
        if self.state["ring_hidden"]:
            self.hide_card()
            self.root.withdraw()
            if self.codex_shown:
                self.codex_win.withdraw()
                self.codex_shown = False
        else:
            self.root.deiconify()
            self.frame_key = self.codex_key = None  # draw them afresh

    def show_ring(self):
        if not self.ring_var.get():
            self.ring_var.set(True)
            self.toggle_ring()

    def toggle_autostart(self):
        self.state["autostart"] = bool(self.autostart_var.get())
        self.save_state()
        try:
            set_autostart(self.state["autostart"])
        except OSError as e:
            log(f"autostart: {e}")

    def set_lang(self):
        LANG[0] = self.state["lang"] = self.lang_var.get()
        self.save_state()
        self.base_key = None
        self.tray.key = None
        self.card_next = 0.0

    def poll_tray(self):
        try:
            trays = [(self.tray, "tray")]
            if getattr(self, "codex_tray", None):
                trays.append((self.codex_tray, "codex_tray"))
            for tray, anchor in trays:
                while tray.events:
                    event = tray.events.popleft()
                    if event == "click":
                        self.tray_click()
                    elif event == "menu":
                        self.tray_menu()
                    elif event == "hover":
                        self.show_card(anchor)
                    elif event == "leave":
                        self.hide_card()
                    elif event == "show":
                        self.show_ring()
        except Exception:
            log(traceback.format_exc())
        self.root.after(100, self.poll_tray)

    def tray_click(self):
        self.ring_var.set(not self.ring_var.get())
        self.toggle_ring()

    def tray_menu(self):
        self.hide_card()
        self.build_menu()
        p = wintypes.POINT()
        user32.GetCursorPos(ctypes.byref(p))
        user32.SetForegroundWindow(self.tray.hwnd)  # so the menu closes when you click away
        self.menu.tk_popup(p.x, p.y)

    def tray_tip(self, v):
        if v.left is None:
            return f"{T('title')}: {T('no_reading')}"
        tip = f"{T('title')}: {round(v.left)}% {T('left_word')}"
        return tip + (f" · {v.reset_text}" if v.resets else "")

    def on_menu(self, e):
        self.hide_card()
        self.build_menu()
        self.menu.tk_popup(e.x_root, e.y_root)

    def toggle_mute(self):
        self.state["muted"] = not self.state.get("muted", False)
        self.siren.muted = self.state["muted"]
        self.save_state()

    def toggle_chime(self):
        self.state["chime"] = bool(self.chime_on.get())
        self.save_state()

    def hear(self, pct):
        """Plays the siren's pitch for `pct` left for a few seconds, then goes back."""
        self.preview = (pct, time.time() + 5)

    # dragging and hovering -------------------------------------------------

    def clamp_size(self, size):
        return max(self.min_size, min(self.max_size, size))

    def on_grip(self, x, y):
        c = self.size - self.grip_r - 2
        return (x - c) ** 2 + (y - c) ** 2 <= (self.grip_r + 3) ** 2

    def on_motion(self, e):
        if not self.drag and not self.resizing:
            self.root.config(cursor="size_nw_se" if self.on_grip(e.x, e.y) else "")

    def on_press(self, e):
        self.hide_card()
        if self.on_grip(e.x, e.y):
            self.resizing = (self.size, e.x_root, e.y_root)
        else:
            self.drag = (e.x_root - self.root.winfo_x(), e.y_root - self.root.winfo_y())

    def on_drag(self, e):
        if self.resizing:  # a quick stretched preview; the sharp redraw comes on release
            start, x0, y0 = self.resizing
            size = self.clamp_size(start + max(e.x_root - x0, e.y_root - y0))
            if size != self.size and self.last_img is not None:
                self.size = size
                self.root.geometry(f"{size}x{size}")
                blit(self.hwnd, self.last_img.resize((size, size), Image.BILINEAR))
                if self.codex_shown and self.codex_last_img is not None:  # the Codex ring keeps pace
                    self.place_codex()
                    blit(self.codex_hwnd, self.codex_last_img.resize((size, size), Image.BILINEAR))
        elif self.drag:
            self.root.geometry(f"+{e.x_root - self.drag[0]}+{e.y_root - self.drag[1]}")
            self.place_codex()

    def on_release(self, _e):
        if self.resizing:
            self.resizing = None
            self.set_size(self.size)
        elif self.drag:
            self.drag = None
            self.state["x"], self.state["y"] = self.root.winfo_x(), self.root.winfo_y()
            self.save_state()

    def on_double(self, e):
        if self.on_grip(e.x, e.y):
            self.resizing = None
            self.set_size(int(BASE * self.scale))  # back to the usual size
        else:
            self.toggle_mute()

    def set_size(self, size):
        self.size = self.clamp_size(size)
        self.ring = Ring(self.size, tag="Claude")
        # Grown past the screen's edge: slide back so all of it shows.
        left, top, right, bottom = work_area(self.hwnd)
        x = max(left, min(self.root.winfo_x(), right - self.size))
        y = max(top, min(self.root.winfo_y(), bottom - self.size))
        self.root.geometry(f"{self.size}x{self.size}+{x}+{y}")
        self.state["x"], self.state["y"] = x, y
        self.state["ring_scale"] = round(self.size / (BASE * self.scale), 3)
        self.save_state()
        self.base_key = self.frame_key = None  # draw it afresh at the new size
        self.codex_ring = Ring(self.size, accent=TEAL, tag="Codex", comet=CODEX_COMET)
        self.codex_key = None
        self.place_codex()
        self.refresh()  # both rings sharp at once, not on the next tick

    def on_enter(self, _e):
        if self.hover_job is None and not self.drag:
            self.hover_job = self.root.after(300, lambda: self.show_card("ring"))

    def hide_card(self):
        if self.hover_job is not None:
            self.root.after_cancel(self.hover_job)
            self.hover_job = None
        if self.card_shown:
            self.card.withdraw()
            self.card_shown = False

    def show_card(self, anchor=None):
        self.hover_job = None
        if anchor:
            self.card_anchor = anchor
        v = self.codex_view if self.card_anchor in ("codex", "codex_tray") else self.view
        if v is None:
            return
        img = self.card_painter.render(v, self.state.get("muted", False), self.siren.volume)
        w, h = img.size
        gap = int(10 * self.scale)
        if self.card_anchor in ("tray", "codex_tray"):  # above the icon (or below, by a taskbar at the top)
            l, t, r, b = (self.codex_tray if self.card_anchor == "codex_tray" else self.tray).rect()
            left, top, right, bottom = work_area_at((l + r) // 2, (t + b) // 2)
            x = min(max((l + r) // 2 - w // 2, left + gap), right - w - gap)
            y = bottom - h - gap if (t + b) / 2 > (top + bottom) / 2 else top + gap
        else:  # beside the ring it is about
            win = self.codex_win if self.card_anchor == "codex" else self.root
            rx, ry = win.winfo_x(), win.winfo_y()
            left, top, right, bottom = work_area(self.hwnd)
            x = rx - w - gap if rx - w - gap >= left else rx + self.size + gap
            y = min(max(ry + self.size // 2 - h // 2, top + gap), bottom - h - gap)
        self.card.geometry(f"{w}x{h}+{x}+{y}")
        if not self.card_shown:
            self.card.deiconify()
            self.card_shown = True
        blit(self.card_hwnd, img)
        self.card_next = time.time() + 1

    # loop ----------------------------------------------------------------

    def tick(self):
        delay = 1000
        try:
            delay = self.refresh()
        except Exception:
            log(traceback.format_exc())
        self.root.after(delay, self.tick)

    def read(self, now):
        try:
            mtime = USAGE.stat().st_mtime
        except OSError:
            mtime = None
        if mtime != self.mtime:
            self.mtime = mtime
            self.usage = read_json(USAGE) or {}

        prune = now >= self.next_prune
        if prune:
            self.next_prune = now + 600
        sessions = []
        for path in SESSIONS.glob("*.json"):
            s = read_json(path)
            if not isinstance(s, dict):
                continue
            if prune and now - s.get("updatedAt", 0) / 1000 > 2 * 86400:
                path.unlink(missing_ok=True)  # a session long gone
                continue
            s["_id"] = path.stem
            sessions.append(s)
        self.sessions = sessions

        for s in sessions:  # a long reply just finished: chime and flash
            done, seen = s.get("doneAt", 0), self.done_seen.get(s["_id"])
            self.done_seen[s["_id"]] = done
            if seen is not None and done > seen and s.get("lastDurationMs", 0) >= CHIME_AFTER * 1000:
                self.flash_until = now + 2.5
                if self.chime_on.get():
                    self.siren.ring()

        self.view = View(self.usage, sessions, now)

    def refresh(self):
        now = time.time()
        if now >= self.next_read:
            self.next_read = now + 1
            self.read(now)
        v = self.view
        muted = self.state.get("muted", False)

        if self.preview and now >= self.preview[1]:
            self.preview = None
        self.siren.left = self.preview[0] if self.preview else v.left
        self.siren.muted = False if self.preview else muted

        line, line_color = v.ring_line()
        if self.refresh_asked:
            if self.usage.get("updatedAt", 0) / 1000 >= self.refresh_asked:
                self.refresh_asked, self.refresh_note = 0.0, (T("refreshed"), GREEN, now + 2.5)
            elif now - self.refresh_asked > 20:  # no session open to answer
                self.refresh_asked, self.refresh_note = 0.0, (T("no_session"), AMBER, now + 4)
            else:
                line, line_color = T("refreshing"), SOFT
        if self.refresh_note and now < self.refresh_note[2]:
            line, line_color = self.refresh_note[:2]
        if now < self.vol_until:
            line, line_color = T("vol", p=pct(self.siren.volume * 100)), WHITE
        base_key = (v.left, line, line_color, v.weekly_left, muted, LANG[0])
        if base_key != self.base_key:
            self.base_key = base_key
            self.base = self.ring.base(v.left, line, line_color, v.weekly_left, muted)

        glow_color, glow = None, 0.0
        if now < self.flash_until:
            glow_color, glow = GREEN, (self.flash_until - now) / 2.5
        elif v.left is not None and v.left <= 10:
            glow_color = color_for(v.left)
            glow = 0.25 + 0.75 * abs(1 - 2 * ((now * 2.5) % 1.0))
        frame = int((now % 1.2) / 1.2 * FRAMES) if v.working else None

        frame_key = (base_key, glow_color, round(glow, 2), frame, v.old)
        if frame_key != self.frame_key and not self.resizing:
            self.frame_key = frame_key
            img = self.ring.compose(self.base, glow_color, glow, frame, v.old)
            g = self.size - 2 * self.grip_r - 2
            img.alpha_composite(self.grip, (g, g))
            self.last_img = img
            blit(self.hwnd, img)

        self.refresh_codex(now)
        self.tray.update(v.left, v.old, self.tray_tip(v))
        if self.card_shown and now >= self.card_next:
            self.show_card()

        animating = v.working or glow_color is not None or self.codex_busy
        return 50 if animating else 1000

    # the Codex ring ---------------------------------------------------------

    def make_codex(self):
        self.codex = CodexWatch()
        self.codex_ring = Ring(self.size, accent=TEAL, tag="Codex", comet=CODEX_COMET)
        saved = read_json(CODEX_HISTORY) or {}
        self.codex_hist, self.codex_window = saved.get("history", []), saved.get("window")
        self.codex_view = self.codex_key = self.codex_base_key = None
        self.codex_last_img = None
        self.codex_busy = False
        self.codex_next = 0.0
        self.codex_shown = False
        self.codex_tray = Tray(self, "ClaudeSessionLimitTrayCodex", accent=TEAL, disc=True, visible=False)
        self.remotes = []
        # Off until the person turns it on: it keeps an SSH session open to each server.
        self.codex_remote_var = tk.BooleanVar(value=self.state.get("codex_remote", False))
        if self.codex_remote_var.get():
            self.start_remotes()
        w = self.codex_win = tk.Toplevel(self.root)
        w.overrideredirect(True)
        w.attributes("-topmost", True)
        w.geometry("1x1+-10000+-10000")
        w.update_idletasks()
        wid = w.winfo_id()
        self.codex_hwnd = user32.GetParent(wid) or wid
        make_layered(self.codex_hwnd)
        blit(self.codex_hwnd, Image.new("RGBA", (1, 1), (0, 0, 0, 0)))
        w.withdraw()
        w.bind("<ButtonPress-1>", self.on_press_codex)  # dragging it moves both rings
        w.bind("<B1-Motion>", self.on_drag)
        w.bind("<ButtonRelease-1>", self.on_release)
        w.bind("<Button-3>", self.on_menu)
        w.bind("<Enter>", lambda e: self.on_enter_codex())
        w.bind("<Leave>", lambda e: self.hide_card())

    def start_remotes(self):
        self.remotes = [RemoteCodex(*server) for server in codex_servers()]
        for r in self.remotes:
            r.start()

    def stop_remotes(self):
        for r in self.remotes:
            r.stop()
        self.remotes = []

    def toggle_remotes(self):
        self.state["codex_remote"] = bool(self.codex_remote_var.get())
        self.save_state()
        self.stop_remotes()
        if self.state["codex_remote"]:
            self.start_remotes()

    def on_press_codex(self, e):
        self.hide_card()
        self.drag = (e.x_root - self.root.winfo_x(), e.y_root - self.root.winfo_y())

    def on_enter_codex(self):
        if self.hover_job is None and not self.drag:
            self.hover_job = self.root.after(300, lambda: self.show_card("codex"))

    def place_codex(self):
        """Beside the Claude ring, on whichever side the screen has room."""
        if not getattr(self, "codex_shown", False):
            return
        gap = int(8 * self.scale)
        rx, ry = self.root.winfo_x(), self.root.winfo_y()
        left, top, right, bottom = work_area(self.hwnd)
        x = rx - self.size - gap if rx - self.size - gap >= left else rx + self.size + gap
        self.codex_win.geometry(f"{self.size}x{self.size}+{x}+{ry}")

    def note_codex(self):
        """Keeps a history of Codex's readings for its graph and forecast."""
        wins = codex_windows(self.codex.reading)
        if not wins:
            return
        main, at = wins[0], self.codex.at
        resets = main.get("resets_at") or 0
        if self.codex_window is None or abs(resets - self.codex_window) > 900:
            self.codex_hist, self.codex_window = [], resets  # a new window starts over
        used = float(main.get("used_percent", 0))
        last = self.codex_hist[-1] if self.codex_hist else None
        if not last or last[1] != used or at - last[0] >= 1800:
            self.codex_hist = (self.codex_hist + [[at, used]])[-400:]
            try:
                CODEX_HISTORY.write_text(json.dumps({"window": self.codex_window, "history": self.codex_hist}),
                                         encoding="utf-8")
            except OSError as e:
                log(f"codex history: {e}")

    def refresh_codex(self, now):
        try:
            if now >= self.codex_next:
                self.codex_next = now + 1
                fresh = self.codex.poll(now)
                for r in self.remotes:  # the account is one: the newest reading anywhere is right
                    if r.state:
                        fresh = self.codex.offer(r.state.get("limits"), r.state.get("at")) or fresh
                if fresh:
                    self.note_codex()
            if self.codex.reading is None:  # Codex has never run here, or left no limits
                if self.codex_shown:
                    self.codex_win.withdraw()
                    self.codex_shown = False
                return
            where = next((r.name for r in self.remotes if r.busy(now)), None)
            self.codex_busy = self.codex.busy(now) or where is not None
            v = self.codex_view = CodexView(self.codex.reading, self.codex.at,
                                            [tuple(p) for p in self.codex_hist], now, self.codex_busy)
            v.where = where
            tip = f"Codex: {round(v.left)}% {T('left_word')}" if v.left is not None else "Codex"
            self.codex_tray.update(v.left, v.old, tip + (f" · {v.reset_text}" if v.resets else ""))
            line, color = v.ring_line()
            base_key = (v.left, line, color, v.weekly_left, LANG[0], self.size)
            if base_key != self.codex_base_key:
                self.codex_base_key = base_key
                self.codex_base = self.codex_ring.base(v.left, line, color, v.weekly_left, False)
            frame = int((now % 1.2) / 1.2 * FRAMES) if v.working else None
            key = (base_key, v.old, frame)
            if key != self.codex_key and not self.resizing:
                self.codex_key = key
                self.codex_last_img = self.codex_ring.compose(self.codex_base, None, 0, frame, v.old)
                blit(self.codex_hwnd, self.codex_last_img)
            if not self.codex_shown and self.ring_var.get():
                self.codex_shown = True
                self.place_codex()
                self.codex_win.deiconify()
        except Exception:
            log(traceback.format_exc())

    def run(self):
        try:
            self.root.mainloop()
        finally:
            self.tray.remove()
            if getattr(self, "codex_tray", None):
                self.codex_tray.remove()
            if getattr(self, "remotes", None):
                self.stop_remotes()


if __name__ == "__main__":
    try:
        Widget().run()
    except Exception:
        log(traceback.format_exc())
        raise
