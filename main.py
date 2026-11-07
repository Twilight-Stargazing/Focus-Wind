"""Focus Wind - ứng dụng desktop độc lập (Python 3 + WebView2, chạy offline)."""
import asyncio
import base64
import ctypes
import json
import math
import os
import shutil
import subprocess
import sys
import threading
import time
import traceback
import unicodedata
from ctypes import wintypes as wt
from pathlib import Path

try:  # DPI chuẩn để cửa sổ không bị mờ
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    pass

_WV_ARGS = "--disable-features=HardwareMediaKeyHandling,MediaSessionService"


def _low_ram():  # máy có RAM thấp (<= 4,5 GB tổng; máy "4 GB" thường báo 3,4-4 GB)
    class MS(ctypes.Structure):
        _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong), ("ullTotalPhys", ctypes.c_ulonglong),
                    ("ullAvailPhys", ctypes.c_ulonglong), ("ullTotalPageFile", ctypes.c_ulonglong),
                    ("ullAvailPageFile", ctypes.c_ulonglong), ("ullTotalVirtual", ctypes.c_ulonglong),
                    ("ullAvailVirtual", ctypes.c_ulonglong), ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]
    try:
        s = MS()
        s.dwLength = ctypes.sizeof(MS)
        return bool(ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(s))) and 0 < s.ullTotalPhys <= 4.5 * 2**30
    except Exception:
        return False


# "Tối ưu RAM" (do người dùng bật, hoặc tự bật 1 lần trên máy RAM thấp) -> bớt tiến trình/bộ nhớ của WebView2.
# Phải đặt trước khi cửa sổ được tạo, nên đọc cờ ngay từ file lưu. Cờ "ra" = đã tự quyết 1 lần, để người dùng tắt đi thì không bật lại.
_st = Path(os.environ.get("APPDATA", Path.home())) / "Focus Wind" / "state.json"
_d = {}
try:
    _d = json.loads(_st.read_text(encoding="utf-8"))
except FileNotFoundError:
    pass
except Exception:
    _d = None  # file đọc lỗi: không đụng vào
_AUTO_RAM = isinstance(_d, dict) and not _d.get("ra") and _low_ram()
if _AUTO_RAM or (isinstance(_d, dict) and _d.get("ram")):
    # shortcut: cờ WebView2 chưa thử trên nhiều máy; nếu app lỗi/lag thì xóa 2 cờ cuối, giữ --disable-gpu
    _WV_ARGS += " --disable-gpu --enable-low-end-device-mode --renderer-process-limit=1"
os.environ.setdefault("WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS", _WV_ARGS)
import webview
from version import APP_VERSION, UPDATE_URL

ROOT = Path(getattr(sys, "_MEIPASS", Path(__file__).parent))
_APPDATA = Path(os.environ.get("APPDATA", Path.home()))
DATA = _APPDATA / "Focus Wind"  # thư mục dữ liệu mới (ghi chú, cài đặt, nhật ký)
_OLD_DATA = _APPDATA / "RWindUI"  # tên thư mục của các bản cũ
if not DATA.exists() and _OLD_DATA.is_dir():
    try:  # chuyển dữ liệu bản cũ sang tên mới; lỗi thì bỏ qua để app vẫn mở bình thường
        shutil.copytree(_OLD_DATA, DATA)
    except Exception:
        pass
DATA.mkdir(parents=True, exist_ok=True)
STATE = DATA / "state.json"
if _AUTO_RAM:  # lần đầu trên máy RAM thấp: bật sẵn Tối ưu RAM (giao diện đọc cờ này từ file)
    try:
        STATE.write_text(json.dumps({**_d, "ra": 1, "ram": True, "fx": False}, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass
LOG = DATA / "island.log"  # nhật ký nhỏ để dò lỗi nếu Island vẫn không hiện
MP_FILE = DATA / "music.json"  # Trình phát nhạc: playlist + cài đặt (tách khỏi state.json để Glint khỏi nạp lại mỗi lần đổi bài)
_save_lock = threading.Lock()  # ghi state.json an toàn khi nhiều luồng cùng ghi
MAIN_WIN = None  # cửa sổ chính, để báo cho giao diện khi Glint bị bật/tắt bằng phím tắt
MP_NOW = {}  # bài đang có trong Trình phát (giao diện báo sang): title, artist, src, playing, t

# Dùng bản DLL RIÊNG: ctypes.windll.* là bản dùng chung với pywebview, đặt argtypes lên đó sẽ làm pywebview lỗi.
U, G, DWM, WINMM, SH = (ctypes.WinDLL(n) for n in ("user32", "gdi32", "dwmapi", "winmm", "shell32"))
K = ctypes.WinDLL("kernel32")


class MARGINS(ctypes.Structure):
    _fields_ = [(n, ctypes.c_int) for n in ("l", "r", "t", "b")]


class MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", wt.DWORD), ("rcMonitor", wt.RECT), ("rcWork", wt.RECT), ("dwFlags", wt.DWORD)]


U.FindWindowW.restype = wt.HWND
U.FindWindowW.argtypes = [wt.LPCWSTR, wt.LPCWSTR]
U.FindWindowExW.restype = wt.HWND
U.FindWindowExW.argtypes = [wt.HWND, wt.HWND, wt.LPCWSTR, wt.LPCWSTR]
U.GetWindowThreadProcessId.restype = wt.DWORD
U.GetWindowThreadProcessId.argtypes = [wt.HWND, ctypes.POINTER(wt.DWORD)]
U.SetWindowPos.argtypes = [wt.HWND, wt.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wt.UINT]
U.GetWindowRect.argtypes = [wt.HWND, ctypes.POINTER(wt.RECT)]
U.SetWindowRgn.argtypes = [wt.HWND, wt.HANDLE, wt.BOOL]
U.GetWindowLongW.argtypes = [wt.HWND, ctypes.c_int]
U.SetWindowLongW.argtypes = [wt.HWND, ctypes.c_int, ctypes.c_long]
U.ShowWindow.argtypes = [wt.HWND, ctypes.c_int]
U.SetForegroundWindow.argtypes = [wt.HWND]
U.GetCursorPos.argtypes = [ctypes.POINTER(wt.POINT)]
U.GetAsyncKeyState.argtypes = [ctypes.c_int]
U.GetAsyncKeyState.restype = ctypes.c_short
U.GetSystemMetrics.argtypes = [ctypes.c_int]
U.MonitorFromPoint.argtypes = [wt.POINT, wt.DWORD]
U.MonitorFromPoint.restype = wt.HANDLE
U.GetMonitorInfoW.argtypes = [wt.HANDLE, ctypes.POINTER(MONITORINFO)]
U.RegisterHotKey.argtypes = [wt.HWND, ctypes.c_int, wt.UINT, wt.UINT]
U.RegisterHotKey.restype = wt.BOOL
U.UnregisterHotKey.argtypes = [wt.HWND, ctypes.c_int]
U.PostThreadMessageW.argtypes = [wt.DWORD, wt.UINT, wt.WPARAM, wt.LPARAM]
U.GetMessageW.argtypes = [ctypes.POINTER(wt.MSG), wt.HWND, wt.UINT, wt.UINT]
U.GetMessageW.restype = ctypes.c_int
U.WaitMessage.argtypes = []
U.TranslateMessage.argtypes = [ctypes.POINTER(wt.MSG)]
U.DispatchMessageW.argtypes = [ctypes.POINTER(wt.MSG)]
K.GetCurrentThreadId.restype = wt.DWORD
U.keybd_event.argtypes = [wt.BYTE, wt.BYTE, wt.DWORD, ctypes.c_size_t]
WNDENUMPROC = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
U.EnumWindows.argtypes = [WNDENUMPROC, wt.LPARAM]
U.IsWindowVisible.argtypes = [wt.HWND]
U.IsIconic.argtypes = [wt.HWND]
U.GetWindowTextLengthW.argtypes = [wt.HWND]
U.GetWindowTextW.argtypes = [wt.HWND, wt.LPWSTR, ctypes.c_int]
G.CreateRoundRectRgn.restype = G.CreateRectRgn.restype = G.CreateEllipticRgn.restype = wt.HANDLE
G.CreateRoundRectRgn.argtypes = [ctypes.c_int] * 6
G.CreateRectRgn.argtypes = G.CreateEllipticRgn.argtypes = [ctypes.c_int] * 4
G.CombineRgn.argtypes = [wt.HANDLE, wt.HANDLE, wt.HANDLE, ctypes.c_int]
G.DeleteObject.argtypes = [wt.HANDLE]
SH.SHQueryUserNotificationState.argtypes = [ctypes.POINTER(ctypes.c_int)]
DWM.DwmSetWindowAttribute.argtypes = [wt.HWND, wt.DWORD, ctypes.c_void_p, wt.DWORD]
DWM.DwmExtendFrameIntoClientArea.argtypes = [wt.HWND, ctypes.POINTER(MARGINS)]

# class style (CS_DROPSHADOW nằm ở đây): Ptr trên Python 64-bit, bản thường trên 32-bit
_gcl = getattr(U, "GetClassLongPtrW", None) or U.GetClassLongW
_scl = getattr(U, "SetClassLongPtrW", None) or U.SetClassLongW
_gcl.argtypes, _gcl.restype = [wt.HWND, ctypes.c_int], ctypes.c_size_t
_scl.argtypes, _scl.restype = [wt.HWND, ctypes.c_int, ctypes.c_size_t], ctypes.c_size_t

KEYS = {"prev": 0xB1, "play": 0xB3, "next": 0xB0}  # phím media của Windows
NOSIZE, NOMOVE, NOZORDER, NOACTIVATE, FRAMECHANGED, SHOW = 0x1, 0x2, 0x04, 0x10, 0x20, 0x40

# Hotkey constants
MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008
MOD_NOREPEAT = 0x4000  # giữ phím không bắn lặp
# tên chức năng -> phím mặc định (người dùng đổi được trong Cài đặt)
HK_ACTIONS = {
    "glint": "ctrl+shift+i",
    "music_play": "ctrl+alt+space",
    "music_next": "ctrl+alt+right",
    "music_prev": "ctrl+alt+left",
    "pomo_toggle": "ctrl+alt+p",
    "pomo_skip": "ctrl+alt+s",
    "pomo_reset": "ctrl+alt+r",
    "note_add": "ctrl+alt+n",
    "amb_toggle": "ctrl+alt+m",
}
HK_IDS = {n: 0xBEEF + i for i, n in enumerate(HK_ACTIONS)}
HK_BY_ID = {v: k for k, v in HK_IDS.items()}
HK_JS = {  # chức năng chạy trong cửa sổ chính
    "pomo_toggle": "window.pomoToggle&&window.pomoToggle()",
    "pomo_skip": "window.pomoSkip&&window.pomoSkip()",
    "pomo_reset": "window.pomoReset&&window.pomoReset()",
    "note_add": "window.noteAdd&&window.noteAdd()",
    "amb_toggle": "window.ambToggle&&window.ambToggle()",
}
HK_MEDIA = {"music_play": "play", "music_next": "next", "music_prev": "prev"}

# Default state keys for validation
STATE_KEYS = {'notes', 'acc', 'bg1', 'bg2', 'ib1', 'ib2', 'isl', 'fx', 'seen', 'pt', 'nt', 'ag', 'pomo', 'hk', 'font',
              'mood', 'gp', 'presets', 'sm', 'sp', 'lnk', 'hks', 'ap'}


def log(*a):
    try:
        if LOG.exists() and LOG.stat().st_size > 50_000:
            LOG.unlink()
        with LOG.open("a", encoding="utf-8") as f:
            f.write(time.strftime("%H:%M:%S ") + " ".join(map(str, a)) + "\n")
    except Exception:
        pass


def safe(fn):
    """Bọc hàm chạy ở luồng phụ: lỗi được ghi vào island.log thay vì im lặng biến mất."""
    def run(*a):
        try:
            return fn(*a)
        except Exception:
            log(traceback.format_exc())
    return run


def read_state():
    try:
        return json.loads(STATE.read_text(encoding="utf-8"))
    except Exception:
        return {}


_FLAGS = {"sig": None, "d": {}}


def state_flags():
    """Chỉ lấy các cờ isl/ag/fx và chỉ đọc lại file khi nó đổi (game_tick gọi mỗi 1,5 giây, không cần parse lại cả state)."""
    try:
        s = STATE.stat()
        sig = (s.st_mtime_ns, s.st_size)
        if sig != _FLAGS["sig"]:
            st = json.loads(STATE.read_text(encoding="utf-8"))
            _FLAGS["d"] = {k: st[k] for k in ("isl", "ag", "fx") if k in st}
            _FLAGS["sig"] = sig
        return _FLAGS["d"]
    except Exception:
        return {}


def find(title, tries=100):
    """Tìm cửa sổ theo tiêu đề, chỉ lấy cửa sổ của chính app này (tránh nhầm với cửa sổ khác trùng tên, ví dụ thư mục tên 'Glint')."""
    pid = os.getpid()
    for _ in range(tries):
        h = None
        while True:
            h = U.FindWindowExW(None, h, None, title)
            if not h:
                break
            owner = wt.DWORD(0)
            U.GetWindowThreadProcessId(h, ctypes.byref(owner))
            if owner.value == pid:
                return h
        time.sleep(0.1)
    return None


def dwm(h, attr, value):
    v = ctypes.c_uint(value)
    DWM.DwmSetWindowAttribute(h, attr, ctypes.byref(v), 4)


def style(h, idx, clear=0, add=0):
    v = (U.GetWindowLongW(h, idx) & 0xFFFFFFFF & ~clear) | add
    U.SetWindowLongW(h, idx, ctypes.c_long(v).value)


def colorref(hexs):
    """'#rrggbb' -> COLORREF (0x00BBGGRR); None nếu không hợp lệ."""
    s = (hexs or "").lstrip("#")
    if len(s) != 6:
        return None
    try:
        r, g, b = int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16)
    except ValueError:
        return None
    return r | (g << 8) | (b << 16)


def paint_titlebar(h, dark, bg="", fg=""):
    """Thanh tiêu đề theo màu app.
    - Windows 10: chỉ có chế độ tối/sáng (thuộc tính 20 cho bản 2004+, 19 cho bản cũ hơn), nên thử cả hai.
    - Windows 11: tô đúng màu nền (35), màu chữ (36), màu viền (34).
    Cuối cùng ép Windows vẽ lại khung (FRAMECHANGED), nếu không thanh tiêu đề có thể vẫn giữ màu trắng cũ."""
    attempt(dwm, h, 20, int(bool(dark)))
    attempt(dwm, h, 19, int(bool(dark)))
    cb, cf = colorref(bg), colorref(fg)
    if cb is not None:
        attempt(dwm, h, 35, cb)
        attempt(dwm, h, 34, cb)
    if cf is not None:
        attempt(dwm, h, 36, cf)
    U.SetWindowPos(h, None, 0, 0, 0, 0, NOMOVE | NOSIZE | NOZORDER | NOACTIVATE | FRAMECHANGED)


def attempt(fn, *a):
    try:
        fn(*a)
    except Exception:
        log(traceback.format_exc())


# --- Hộp thoại "Lưu thành..." của Windows, chạy trên luồng STA riêng ---
# (js_api của pywebview chạy trên luồng HTTP nên KHÔNG được gọi create_file_dialog ở đó)
class OPENFILENAMEW(ctypes.Structure):
    _fields_ = [
        ("lStructSize", wt.DWORD), ("hwndOwner", wt.HWND), ("hInstance", wt.HINSTANCE),
        ("lpstrFilter", wt.LPCWSTR), ("lpstrCustomFilter", wt.LPWSTR), ("nMaxCustFilter", wt.DWORD),
        ("nFilterIndex", wt.DWORD), ("lpstrFile", wt.LPWSTR), ("nMaxFile", wt.DWORD),
        ("lpstrFileTitle", wt.LPWSTR), ("nMaxFileTitle", wt.DWORD), ("lpstrInitialDir", wt.LPCWSTR),
        ("lpstrTitle", wt.LPCWSTR), ("Flags", wt.DWORD), ("nFileOffset", wt.WORD),
        ("nFileExtension", wt.WORD), ("lpstrDefExt", wt.LPCWSTR), ("lCustData", wt.LPARAM),
        ("lpfnHook", ctypes.c_void_p), ("lpTemplateName", wt.LPCWSTR),
        ("pvReserved", ctypes.c_void_p), ("dwReserved", wt.DWORD), ("FlagsEx", wt.DWORD),
    ]


CD = ctypes.WinDLL("comdlg32")
CD.GetSaveFileNameW.argtypes = [ctypes.POINTER(OPENFILENAMEW)]
CD.GetSaveFileNameW.restype = wt.BOOL
OLE = ctypes.WinDLL("ole32")
_save_dlg_lock = threading.Lock()


def ask_save_path(default_name="FocusWind_Notes.md", owner=None, title="Lưu tệp",
                  filt="Markdown (*.md)\0*.md\0Tất cả tệp (*.*)\0*.*\0\0", defext="md", initdir=None):
    """Trả về đường dẫn người dùng chọn, "" nếu hủy, None nếu đang bận/lỗi."""
    if not _save_dlg_lock.acquire(blocking=False):
        return None
    try:
        out = {"path": None}
        done = threading.Event()

        def run():
            try:
                try:
                    OLE.CoInitializeEx(None, 0x2)  # STA cho hộp thoại shell
                except Exception:
                    pass
                buf = ctypes.create_unicode_buffer(1024)
                buf.value = default_name
                ofn = OPENFILENAMEW()
                ofn.lStructSize = ctypes.sizeof(OPENFILENAMEW)
                if owner:
                    ofn.hwndOwner = owner
                ofn.lpstrFilter = filt
                ofn.lpstrFile = ctypes.cast(buf, wt.LPWSTR)
                ofn.nMaxFile = len(buf)
                ofn.lpstrTitle = title
                ofn.lpstrDefExt = defext
                if initdir:
                    ofn.lpstrInitialDir = initdir
                # OVERWRITEPROMPT|HIDEREADONLY|PATHMUSTEXIST|EXPLORER|NOCHANGEDIR
                ofn.Flags = 0x2 | 0x4 | 0x800 | 0x80000 | 0x8
                try:
                    if owner:
                        U.SetForegroundWindow(owner)
                except Exception:
                    pass
                out["path"] = buf.value if CD.GetSaveFileNameW(ctypes.byref(ofn)) else ""
            except Exception:
                log(traceback.format_exc())
                out["path"] = None
            finally:
                done.set()

        threading.Thread(target=run, daemon=True).start()
        done.wait(600)
        return out["path"]
    finally:
        _save_dlg_lock.release()


# --- Trình phát nhạc: chọn file / thả file ---
MP_EXT = {".mp3", ".m4a", ".aac", ".flac", ".wav", ".ogg", ".opus"}  # WebView2 phát được; .wma thì không
MP_MAX = 500  # tối đa bài mỗi danh sách (giao diện giữ cùng con số này)
MP_FILT = "Nhạc (mp3, m4a, aac, flac, wav, ogg, opus)\0*.mp3;*.m4a;*.aac;*.flac;*.wav;*.ogg;*.opus\0Tất cả tệp (*.*)\0*.*\0\0"
CD.GetOpenFileNameW.argtypes = [ctypes.POINTER(OPENFILENAMEW)]
CD.GetOpenFileNameW.restype = wt.BOOL


def ask_open_paths(owner=None, title="Chọn tệp", filt=MP_FILT):
    """Hộp thoại mở nhiều file. Trả về list đường dẫn ([] nếu hủy), None nếu đang bận/lỗi."""
    if not _save_dlg_lock.acquire(blocking=False):
        return None
    try:
        out = {"paths": None}
        done = threading.Event()

        def run():
            try:
                try:
                    OLE.CoInitializeEx(None, 0x2)  # STA cho hộp thoại shell
                except Exception:
                    pass
                buf = ctypes.create_unicode_buffer(1 << 17)  # nhiều file chọn cùng lúc: "thư mục\0file1\0file2\0\0"
                ofn = OPENFILENAMEW()
                ofn.lStructSize = ctypes.sizeof(OPENFILENAMEW)
                if owner:
                    ofn.hwndOwner = owner
                ofn.lpstrFilter = filt
                ofn.lpstrFile = ctypes.cast(buf, wt.LPWSTR)
                ofn.nMaxFile = len(buf)
                ofn.lpstrTitle = title
                # HIDEREADONLY|PATHMUSTEXIST|FILEMUSTEXIST|ALLOWMULTISELECT|EXPLORER|NOCHANGEDIR
                ofn.Flags = 0x4 | 0x800 | 0x1000 | 0x200 | 0x80000 | 0x8
                try:
                    if owner:
                        U.SetForegroundWindow(owner)
                except Exception:
                    pass
                if CD.GetOpenFileNameW(ctypes.byref(ofn)):
                    parts = buf[:].split("\0\0")[0].split("\0")
                    out["paths"] = parts if len(parts) == 1 else [os.path.join(parts[0], f) for f in parts[1:]]
                else:
                    out["paths"] = []
            except Exception:
                log(traceback.format_exc())
                out["paths"] = None
            finally:
                done.set()

        threading.Thread(target=run, daemon=True).start()
        done.wait(600)
        return out["paths"]
    finally:
        _save_dlg_lock.release()


def mp_expand(paths, room=MP_MAX):
    """File nhạc trong các đường dẫn (thư mục thì quét cả thư mục con); dừng khi đủ `room` bài để thả thư mục khổng lồ không làm đứng app."""
    out = []
    for p in paths:
        p = Path(p)
        if p.is_dir():
            for r, ds, fs in os.walk(p):
                ds.sort()
                out += [str(Path(r) / f) for f in sorted(fs) if Path(f).suffix.lower() in MP_EXT]
                if len(out) >= room:
                    return out[:room]
        elif p.is_file() and p.suffix.lower() in MP_EXT:
            out.append(str(p))
    return out[:room]


# --- Khởi động cùng Windows + tạo shortcut ---
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_NAME = "FocusWind"


def launch_target():
    """(file chạy, tham số) để mở lại chính app này: bản đã build (exe) hoặc chạy bằng Python."""
    if getattr(sys, "frozen", False):
        return sys.executable, ""
    py = Path(sys.executable)
    pw = py.with_name("pythonw.exe")  # pythonw để không hiện cửa sổ đen
    return str(pw if pw.exists() else py), '"%s"' % Path(__file__).resolve()


def autostart_cmd():
    exe, args = launch_target()
    return '"%s"' % exe + (" " + args if args else "")


def autostart_get():
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            v, _ = winreg.QueryValueEx(k, RUN_NAME)
        return str(v).lower() == autostart_cmd().lower()  # đường dẫn cũ (đã dời app) thì coi như chưa bật
    except OSError:
        return False


def autostart_set(on):
    import winreg
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
        if on:
            winreg.SetValueEx(k, RUN_NAME, 0, winreg.REG_SZ, autostart_cmd())
        else:
            try:
                winreg.DeleteValue(k, RUN_NAME)
            except FileNotFoundError:
                pass


def desktop_dir():
    d = Path(os.environ.get("USERPROFILE", Path.home())) / "Desktop"
    return str(d) if d.is_dir() else None


def create_lnk(path):
    """Tạo file .lnk bằng WScript.Shell (có sẵn trong Windows); đường dẫn truyền qua biến môi trường nên không lỗi dấu cách/tiếng Việt."""
    exe, args = launch_target()
    ps = ("$s=(New-Object -ComObject WScript.Shell).CreateShortcut($env:FW_LNK);"
          "$s.TargetPath=$env:FW_EXE;$s.Arguments=$env:FW_ARGS;$s.WorkingDirectory=$env:FW_DIR;"
          "$s.IconLocation=$env:FW_EXE+',0';$s.Description='Focus Wind';$s.Save()")
    env = dict(os.environ, FW_LNK=path, FW_EXE=exe, FW_ARGS=args, FW_DIR=str(Path(exe).parent))
    r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", ps],
                       env=env, capture_output=True, text=True, timeout=30, creationflags=0x08000000)
    if r.returncode != 0 or not Path(path).exists():
        raise RuntimeError((r.stderr or "").strip()[:200] or "Không tạo được shortcut")


def set_glint_state(on):
    """Ghi trạng thái bật/tắt Glint vào state.json và báo cho cửa sổ chính để công tắc trong giao diện khớp với phím tắt."""
    try:
        with _save_lock:
            st = read_state()
            st["isl"] = bool(on)
            tmp = STATE.with_suffix(".tmp")
            tmp.write_text(json.dumps(st, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, STATE)
    except Exception:
        log(traceback.format_exc())
    try:
        if MAIN_WIN:
            MAIN_WIN.evaluate_js("window.setGlint&&window.setGlint(%s)" % ("true" if on else "false"))
    except Exception:
        log(traceback.format_exc())


def toggle_island():
    """Bật/tắt Glint (dùng cho phím tắt toàn cục)."""
    if not ISL.h:
        return
    ISL.auto = False
    on = not ISL.vis

    def job():
        (ISL.show if on else ISL.hide)()
        set_glint_state(on)

    threading.Thread(target=safe(job), daemon=True).start()


def parse_hotkey(s):
    """Parse 'ctrl+shift+i' -> {mod: 6, vk: 0x49}; None nếu không hợp lệ."""
    if isinstance(s, dict):
        return s if s.get("vk") else None
    s = (s or "").lower().replace(" ", "")
    if not s:
        return None
    mod = 0
    vk = 0
    key_map = {
        "alt": MOD_ALT, "control": MOD_CONTROL, "ctrl": MOD_CONTROL,
        "shift": MOD_SHIFT, "win": MOD_WIN, "windows": MOD_WIN,
    }
    vk_map = _vk_name_map()
    for p in s.split("+"):
        if p in key_map:
            mod |= key_map[p]
        elif p in vk_map:
            vk = vk_map[p]
    return {"mod": mod, "vk": vk} if vk else None


_VK_EXTRA = {"left": 0x25, "up": 0x26, "right": 0x27, "down": 0x28, "home": 0x24, "end": 0x23,
             "pageup": 0x21, "pagedown": 0x22, "insert": 0x2D, "delete": 0x2E,
             "enter": 0x0D, "tab": 0x09, "backspace": 0x08}
_VK_SHOW = {"space": "Space", "pageup": "PageUp", "pagedown": "PageDown"}


def _vk_name_map():
    return {**{chr(i).lower(): i for i in range(ord('A'), ord('Z') + 1)},
            **{str(i): 0x30 + i for i in range(10)},
            "space": 0x20, **_VK_EXTRA,
            **{f"f{i}": 0x70 + i - 1 for i in range(1, 25)}}


def hotkey_to_string(key_combo):
    """{mod: 6, vk: 0x49} -> 'Ctrl+Alt+I'."""
    if not key_combo or not isinstance(key_combo, dict):
        return ""
    mod = key_combo.get("mod", 0)
    vk = key_combo.get("vk", 0)
    parts = []
    if mod & MOD_CONTROL: parts.append("Ctrl")
    if mod & MOD_SHIFT: parts.append("Shift")
    if mod & MOD_ALT: parts.append("Alt")
    if mod & MOD_WIN: parts.append("Win")
    for k, v in _vk_name_map().items():
        if v == vk:
            parts.append(_VK_SHOW.get(k) or (k.capitalize() if k in _VK_EXTRA else k.upper()))
            break
    return "+".join(parts)


def hk_string(action, st=None):
    """Phím tắt hiện tại của một chức năng ('' = đã tắt). Ưu tiên cài đặt người dùng, rồi phím cũ của Glint, rồi mặc định."""
    st = st if st is not None else read_state()
    hks = st.get("hks") if isinstance(st.get("hks"), dict) else {}
    if action in hks:
        v = hks[action]
    elif action == "glint" and "hk" in st:
        v = st["hk"]
    else:
        v = HK_ACTIONS[action]
    return hotkey_to_string(parse_hotkey(v)) if v else ""


def press_media(name):
    """Điều khiển nhạc: Trình phát trong app nếu nó đang phát (hoặc vừa tạm dừng <10 phút mà nhạc ngoài không phát), còn lại là nhạc ngoài."""
    mp = MP_NOW
    if mp.get("title") and MAIN_WIN and (mp["playing"] or (time.time() - mp["t"] < 600 and not (now_playing() or {}).get("playing"))):
        MAIN_WIN.evaluate_js(f"window.mpKey&&window.mpKey('{name}')")  # name chỉ là prev/play/next
        return
    if media_control(name):  # cách chính: không hiện khung âm lượng của Windows
        return
    vk = KEYS.get(name)  # dự phòng: giả lập phím media
    if vk:
        U.keybd_event(vk, 0, 0, 0)
        U.keybd_event(vk, 0, 2, 0)


def run_hotkey(name):
    def job():
        if name == "glint":
            toggle_island()
        elif name in HK_MEDIA:
            press_media(HK_MEDIA[name])
        elif name in HK_JS and MAIN_WIN:
            MAIN_WIN.evaluate_js(HK_JS[name])
    threading.Thread(target=safe(job), daemon=True).start()


# --- Hotkey worker: đăng ký + vòng lặp message CÙNG một luồng ---
WM_HOTKEY = 0x0312
WM_APP_RELOAD = 0x8000 + 1
_hk_tid = 0
_hk_gen = 0          # tăng sau mỗi lần đăng ký lại, để hotkey_set biết worker đã xong
_hk_ok = {}          # chức năng -> True/False (đăng ký được không), None nếu đã tắt
_hk_suspended = False  # đang ghi phím mới trong Cài đặt: gỡ hết phím tắt


def hotkey_apply():
    """Báo worker đăng ký lại toàn bộ phím tắt theo state.json (đúng luồng của nó)."""
    if _hk_tid:
        U.PostThreadMessageW(_hk_tid, WM_APP_RELOAD, 0, 0)


def hk_register_all():
    global _hk_gen
    st = read_state()
    for name, hid in HK_IDS.items():
        U.UnregisterHotKey(None, hid)
        combo = None if _hk_suspended else parse_hotkey(hk_string(name, st))
        if not combo:
            _hk_ok[name] = None
            continue
        ok = bool(U.RegisterHotKey(None, hid, combo["mod"] | MOD_NOREPEAT, combo["vk"]))
        _hk_ok[name] = ok
        if not ok:
            log("KHÔNG đăng ký được phím tắt:", name, hotkey_to_string(combo))
    _hk_gen += 1


def hotkey_worker():
    """Luồng riêng: đăng ký phím tắt rồi chờ WM_HOTKEY / lệnh đăng ký lại."""
    global _hk_tid
    _hk_tid = K.GetCurrentThreadId()
    hk_register_all()
    msg = wt.MSG()
    while U.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
        if msg.message == WM_HOTKEY and msg.wParam in HK_BY_ID:
            run_hotkey(HK_BY_ID[msg.wParam])
        elif msg.message == WM_APP_RELOAD:
            hk_register_all()
        U.TranslateMessage(ctypes.byref(msg))
        U.DispatchMessageW(ctypes.byref(msg))


def harden(h):
    """Gỡ MỌI khung/viền/bóng mà Windows hoặc DWM tự thêm quanh cửa sổ (nguyên nhân lộ viền hình chữ nhật)."""
    # kiểu cửa sổ: bỏ caption, viền, thanh kéo, menu hệ thống, nút thu/phóng
    style(h, -16, clear=0x00C00000 | 0x00040000 | 0x00080000 | 0x00020000 | 0x00010000)
    # kiểu mở rộng: bỏ các viền 3D/khung; ẩn khỏi taskbar, không giành focus
    style(h, -20, clear=0x1 | 0x100 | 0x200 | 0x20000 | 0x40000, add=0x80 | 0x08000000)
    # class style: bỏ CS_DROPSHADOW (bóng đổ hình chữ nhật không theo hình viên thuốc)
    try:
        _scl(h, -26, _gcl(h, -26) & ~0x20000)
    except Exception:
        pass
    attempt(dwm, h, 2, 1)            # DWMNCRP_DISABLED: tắt vẽ khung/bóng non-client
    attempt(dwm, h, 3, 1)            # tắt hiệu ứng mở/đóng mặc định của DWM (mình tự làm hiệu ứng riêng)
    attempt(dwm, h, 33, 1)           # Win11: không bo góc kiểu Windows
    attempt(dwm, h, 34, 0xFFFFFFFE)  # Win11: không viền
    # pywebview kéo khung kính vào trong cửa sổ để tạo bóng -> chính là viền hình chữ nhật; thu về 0
    attempt(DWM.DwmExtendFrameIntoClientArea, h, ctypes.byref(MARGINS(0, 0, 0, 0)))
    U.SetWindowPos(h, None, 0, 0, 0, 0, 0x1 | 0x2 | NOZORDER | NOACTIVATE | FRAMECHANGED)


def monitor_rect(x, y):
    """Vùng của màn hình chứa điểm (x, y); None nếu điểm nằm ngoài mọi màn hình."""
    hm = U.MonitorFromPoint(wt.POINT(int(x), int(y)), 0)
    if not hm:
        return None
    mi = MONITORINFO()
    mi.cbSize = ctypes.sizeof(mi)
    return mi.rcMonitor if U.GetMonitorInfoW(hm, ctypes.byref(mi)) else None


def fullscreen_busy():
    """True khi Windows thấy có game / ứng dụng toàn màn hình / đang trình chiếu (QUNS_BUSY, D3D_FULL_SCREEN, PRESENTATION_MODE, APP)."""
    st = ctypes.c_int(0)
    return SH.SHQueryUserNotificationState(ctypes.byref(st)) == 0 and st.value in (2, 3, 4, 7)


def spring(t):  # lò xo giảm chấn: rơi xuống, lố ~13% rồi nảy về chỗ cũ
    return 1 - math.exp(-6.5 * t) * math.cos(9.0 * t) if t < 1 else 1.0


def in_back(t):  # lùi nhẹ rồi bật lên
    return 2.7 * t ** 3 - 1.7 * t ** 2


def out_cubic(t):
    return 1 - (1 - t) ** 3


ISL_W, ISL_H = 376, 76  # kích thước Glint (px logic). Nội dung cao ~46px, căn giữa theo chiều dọc nên dư mỗi bên ~15px. Muốn cao/thấp hơn: đổi ISL_H (68 = gọn nhất, 136 = bản cũ bị thừa). ISL_W phải khớp max-width của #isl trong index.html
ISL_R, ISL_EAR = 34, 18  # bo góc viên thuốc; bề rộng mỗi "tai" cong khi dí sát mép trên


class Island:
    """Cửa sổ viên thuốc: định hình, hiện/ẩn có hiệu ứng, kéo, nhớ vị trí."""

    def __init__(self):
        self.h, self.w, self.ht, self.vis, self.job = None, 0, 0, False, 0
        self.dock, self._rd = False, False  # dock: đang dí sát mép trên (có tai); _rd: dạng đã dùng để cắt vùng gần nhất
        self.auto = False  # đang tự ẩn vì có game / toàn màn hình
        self.ended = 0     # mã lần kéo gần nhất mà giao diện báo đã nhả chuột
        self.lock = threading.Lock()
        self.file = DATA / "island.json"

    # ---------- hình dạng ----------
    def setup(self):
        self.h = h = find("Glint")
        if not h:
            log("KHÔNG tìm thấy cửa sổ 'Glint'")
            return False
        U.ShowWindow(h, 0)  # ẩn trước khi gỡ viền để Windows dựng lại khung sạch
        harden(h)
        self.w = self.ht = 0
        self.fit()
        log("setup ok hwnd=%s size=%dx%d" % (h, self.w, self.ht))
        return True

    def rect(self):
        r = wt.RECT()
        U.GetWindowRect(self.h, ctypes.byref(r))
        return r

    def px(self, v):  # px logic -> px thật (theo DPI hiện tại)
        return round(v * self.ht / ISL_H)

    def region(self, w, ht):
        """Viên thuốc thường; hoặc dạng dí sát mép trên: góc trên vuông + 2 'tai' cong (phần lõm) nối vào mép màn hình."""
        r = self.px(ISL_R)
        if not self.dock:
            return G.CreateRoundRectRgn(0, 0, w + 1, ht + 1, 2 * r, 2 * r)
        e = self.px(ISL_EAR)
        rg = G.CreateRoundRectRgn(e, -r, w - e + 1, ht + 1, 2 * r, 2 * r)  # góc trên bo nằm ngoài cửa sổ nên không thấy
        for x0 in (0, w - e):  # tai = hình vuông e x e trừ hình tròn tâm ở mép ngoài, nên cung lõm chạm mép trên và chạm thân viên thuốc
            cx = x0 if x0 == 0 else x0 + e
            ear, hole = G.CreateRectRgn(x0, 0, x0 + e, e), G.CreateEllipticRgn(cx - e, 0, cx + e, 2 * e)
            G.CombineRgn(ear, ear, hole, 4)  # RGN_DIFF
            G.CombineRgn(rg, rg, ear, 2)  # RGN_OR
            G.DeleteObject(hole)
            G.DeleteObject(ear)
        return rg

    def fit(self):
        """Cắt cửa sổ theo hình dạng hiện tại; làm lại nếu kích thước hoặc dạng đổi (ví dụ chuyển sang màn hình khác DPI)."""
        r = self.rect()
        w, ht = r.right - r.left, r.bottom - r.top
        if (w, ht, self.dock) != (self.w, self.ht, self._rd) and w > 0 and ht > 0:
            self.w, self.ht, self._rd = w, ht, self.dock
            U.SetWindowRgn(self.h, self.region(w, ht), True)

    def set_dock(self, on, x=None, y=None):
        """Đổi giữa viên thuốc thường và dạng dí sát mép trên (cửa sổ rộng thêm 2 tai; nội dung giữ nguyên chỗ vì được căn giữa).
        (x, y) = góc trên-trái của VIÊN THUỐC; bỏ trống = giữ chỗ cũ."""
        r, e = self.rect(), self.px(ISL_EAR)
        k = e if self.dock else 0
        pw = r.right - r.left - 2 * k
        x = r.left + k if x is None else x
        y = r.top if y is None else y
        self.dock = bool(on)
        k = e if self.dock else 0
        U.SetWindowPos(self.h, wt.HWND(-1), int(x - k), int(y), int(pw + 2 * k), r.bottom - r.top, NOACTIVATE)
        self.fit()

    def dock_top(self):
        """Nhấp đúp: bay lên giữa mép trên của màn hình đang chứa Glint rồi dí sát, thêm 2 tai cong hai bên."""
        r, e = self.rect(), self.px(ISL_EAR)
        k = e if self.dock else 0
        pw = self.w - 2 * k
        m = monitor_rect(r.left + self.w // 2, r.top + self.ht // 2)
        left, top, right = (m.left, m.top, m.right) if m else (0, 0, U.GetSystemMetrics(0))
        x = left + (right - left - pw) // 2
        self.glide(x - k, top)
        self.set_dock(True, x, top)
        self.save_pos()

    # ---------- vị trí ----------
    def screen(self):  # toàn bộ vùng màn hình ảo (nhiều màn hình)
        return [U.GetSystemMetrics(i) for i in (76, 77, 78, 79)]

    def home(self):
        return (U.GetSystemMetrics(0) - self.w) // 2, max(12, int(self.ht * 0.3))

    def spot(self):
        """(x, y, dí sát?) của viên thuốc lần trước; không có/không hợp lệ thì về giữa phía trên, dạng thường."""
        try:
            p = json.loads(self.file.read_text())
            x, y = int(p["x"]), int(p["y"])
            if (x, y) != (0, 0) and monitor_rect(x + self.w // 2, y + self.ht // 2):
                return x, y, bool(p.get("dock"))
        except Exception:
            pass
        return (*self.home(), False)

    def save_pos(self):
        try:
            r = self.rect()
            k = self.px(ISL_EAR) if self.dock else 0
            self.file.write_text(json.dumps({"x": r.left + k, "y": r.top, "dock": self.dock}))
        except Exception:
            pass

    def place(self, x, y, flags=0):
        U.SetWindowPos(self.h, wt.HWND(-1), int(x), int(y), 0, 0, NOSIZE | NOACTIVATE | flags)

    def clamp(self, x, y, cx, cy):  # giữ nguyên viên thuốc trong màn hình đang chứa con trỏ
        m = monitor_rect(cx, cy)
        if m:
            l, t, r, b = m.left, m.top, m.right, m.bottom
        else:
            vx, vy, vw, vh = self.screen()
            l, t, r, b = vx, vy, vx + vw, vy + vh
        return min(max(x, l), r - self.w), min(max(y, t), b - self.ht)

    # ---------- hiệu ứng ----------
    def tween(self, ms, step, job):
        """Chạy step(t) với t: 0 -> 1 theo thời gian thật, nên mượt và nhanh như nhau trên mọi máy."""
        WINMM.timeBeginPeriod(1)  # đồng hồ 1ms chỉ trong lúc chạy hiệu ứng
        try:
            t0 = time.perf_counter()
            while job == self.job:
                t = (time.perf_counter() - t0) * 1000 / ms
                if t >= 1:
                    step(1.0)
                    break
                step(t)
                time.sleep(0.004)
        finally:
            WINMM.timeEndPeriod(1)

    def show(self):
        with self.lock:
            if self.vis or not self.h:
                return
            self.job += 1
            job = self.job
            harden(self.h)
            self.fit()
            x, y, dk = self.spot()
            self.set_dock(dk, x, y)  # đang ẩn nên đổi dạng/chỗ không bị nhấp nháy
            r = self.rect()
            x, y = r.left, r.top
            m = monitor_rect(x + self.w // 2, y + self.ht // 2)
            top = m.top if m else 0
            if read_state().get("fx", True):  # rơi từ mép trên xuống, lố một chút rồi nảy về
                y0 = top - self.ht + 3  # còn lộ 3px để WebView2 biết cửa sổ đang hiện và vẽ nội dung
                self.place(x, y0, SHOW)
                time.sleep(0.08)
                self.tween(780, lambda t: self.place(x, y0 + (y - y0) * spring(t)), job)
            self.place(x, y, SHOW)
            self.vis = True
            log("show tại", x, y)

    def hide(self, fast=False):
        with self.lock:
            if not self.vis:
                return
            self.job += 1
            job = self.job
            self.save_pos()
            r = self.rect()
            if not fast and read_state().get("fx", True):  # lùi nhẹ rồi trượt lên mất hút
                m = monitor_rect(r.left + self.w // 2, r.top + self.ht // 2)
                end = (m.top if m else 0) - self.ht - 8
                self.tween(380, lambda t: self.place(r.left, r.top + (end - r.top) * in_back(t)), job)
            U.ShowWindow(self.h, 0)
            self.vis = False

    def glide(self, x1, y1):
        self.job += 1
        job = self.job
        r = self.rect()
        x0, y0 = r.left, r.top
        if read_state().get("fx", True):
            self.tween(450, lambda t: self.place(x0 + (x1 - x0) * out_cubic(t), y0 + (y1 - y0) * out_cubic(t)), job)
        self.place(x1, y1)

    # ---------- kéo thả ----------
    def drag_start(self, seq=0):
        """Bắt đầu kéo: một luồng bám theo con trỏ chuột (êm, có độ trôi) cho tới khi giao diện báo đã nhả chuột."""
        seq = int(seq or 0)
        if not self.h or not self.vis or seq <= self.ended:  # lệnh nhả chuột đến trước lệnh bắt đầu (nhấp rất nhanh)
            return
        self.job += 1
        if self.dock:  # kéo ra khỏi mép trên -> trở lại viên thuốc thường
            self.set_dock(False)
        pt = wt.POINT()
        U.GetCursorPos(ctypes.byref(pt))
        r = self.rect()
        threading.Thread(target=safe(self._follow), args=(self.job, seq, pt.x, pt.y, r.left, r.top), daemon=True).start()

    def drag_end(self, seq=0):
        self.ended = max(self.ended, int(seq or 0))

    def _follow(self, job, seq, sx, sy, ox, oy):
        # Không phụ thuộc hoàn toàn vào GetAsyncKeyState: khi một cửa sổ chạy quyền admin (Task Manager, PowerShell admin)
        # đang ở trên cùng, Windows (UIPI) báo "không nhấn" cho app thường, nên chỉ dùng nó khi đọc được lúc bắt đầu kéo.
        cx, cy = float(ox), float(oy)
        pt = wt.POINT()
        t0 = last = time.perf_counter()
        # Nếu lúc bắt đầu kéo mà đọc được nút chuột (nút đang nhấn thật) thì dùng luôn để biết khi nào nhả,
        # phòng khi giao diện làm rơi mất sự kiện nhả chuột (nguyên nhân Glint tự bám theo chuột). Nếu đọc không được (UIPI) thì bỏ qua.
        seen = bool(U.GetAsyncKeyState(0x01) & 0x8000)
        up_since = 0.0
        WINMM.timeBeginPeriod(1)
        try:
            while job == self.job:
                now = time.perf_counter()
                if seen:
                    if U.GetAsyncKeyState(0x01) & 0x8000:
                        up_since = 0.0
                    elif not up_since:
                        up_since = now
                released = bool(seen and up_since and now - up_since > 0.15)
                held = self.ended < seq and not released and now - t0 < 20  # 20 giây: chốt chặn nếu mất lệnh nhả chuột
                U.GetCursorPos(ctypes.byref(pt))
                tx, ty = self.clamp(ox + pt.x - sx, oy + pt.y - sy, pt.x, pt.y)
                now = time.perf_counter()
                k = 1 - math.exp(-(now - last) * 26)  # độ trôi: cửa sổ đuổi theo chuột chứ không giật
                last = now
                cx, cy = cx + (tx - cx) * k, cy + (ty - cy) * k
                self.place(round(cx), round(cy))
                if not held and abs(tx - cx) < 0.6 and abs(ty - cy) < 0.6:
                    break
                time.sleep(0.005)
        finally:
            WINMM.timeEndPeriod(1)
        if job == self.job:
            self.place(round(cx), round(cy))
            self.fit()
            self.save_pos()

ISL = Island()


SITES = ("YouTube Music", "YouTube", "SoundCloud", "Spotify", "Zing MP3", "NhacCuaTui", "Apple Music", "Deezer", "TikTok", "Facebook")
APPS = (("spotify", "Spotify"), ("brave", "Brave"), ("chrome", "Chrome"), ("msedge", "Edge"), ("edge", "Edge"),
        ("firefox", "Firefox"), ("opera", "Opera"), ("vivaldi", "Vivaldi"), ("coccoc", "Cốc Cốc"),
        ("applemusic", "Apple Music"), ("zune", "Media Player"), ("vlc", "VLC"))
BROWSERS = {"Brave", "Chrome", "Edge", "Firefox", "Opera", "Vivaldi", "Cốc Cốc"}
site_cache = {}
site_miss = {}  # tiêu đề -> thời điểm được dò lại (dò không ra thì nghỉ 10 giây)


def window_titles():
    out = []

    @WNDENUMPROC
    def cb(h, _):
        n = U.GetWindowTextLengthW(h) if U.IsWindowVisible(h) else 0
        if n:
            buf = ctypes.create_unicode_buffer(n + 1)
            U.GetWindowTextW(h, buf, n + 1)
            out.append(buf.value)
        return True

    U.EnumWindows(cb, 0)
    return out


def norm(t):
    return unicodedata.normalize("NFC", t or "").lower()


def media_source(aumid, title):
    """Đoán nơi đang phát: Spotify, YouTube, SoundCloud... (trình duyệt không cho biết trang, nên dò tiêu đề cửa sổ)."""
    a = norm(aumid)
    app = next((n for k, n in APPS if k in a), None) or (aumid or "").split("!")[-1].replace(".exe", "").strip() or None
    if app not in BROWSERS or not title:
        return app
    if title in site_cache:
        return site_cache[title]
    if site_miss.get(title, 0) > time.time():
        return app
    key = norm(title)[:30]
    for t in window_titles():
        t = norm(t)
        if key in t:  # cửa sổ có tab đang phát nhạc
            for site in SITES:
                if site.lower() in t:
                    if len(site_cache) > 50:
                        site_cache.clear()
                    site_cache[title] = site  # nhớ lại, để chuyển sang tab khác vẫn đúng
                    return site
    if len(site_miss) > 50:
        site_miss.clear()
    site_miss[title] = time.time() + 10
    return app  # chưa chắc là trang nào thì hiện tên trình duyệt


def is_own(aumid):
    """Phiên media của chính Focus Wind (WebView2 / python) - không tính là 'nhạc đang phát'."""
    a = norm(aumid)
    return bool(a) and ("webview2" in a or "rwindui" in a or "focuswind" in a or "focus wind" in a
                        or Path(sys.executable).stem.lower() in a)


def pick_session(mgr):
    cur = mgr.get_current_session()
    if cur and not is_own(cur.source_app_user_model_id):
        return cur
    try:
        others = [x for x in mgr.get_sessions() if not is_own(x.source_app_user_model_id)]
    except Exception:
        return None
    for x in others:
        try:
            if int(x.get_playback_info().playback_status) == 4:
                return x
        except Exception:
            pass
    return others[0] if others else None


def now_playing():
    """Đọc bài đang phát từ Windows (Spotify, SoundCloud/YouTube trên trình duyệt...)."""
    try:
        from winrt.windows.media.control import (
            GlobalSystemMediaTransportControlsSessionManager as Manager,
        )

        async def go():
            session = pick_session(await Manager.request_async())
            if not session:
                return None
            info = await session.try_get_media_properties_async()
            playing = int(session.get_playback_info().playback_status) == 4
            try:
                src = media_source(session.source_app_user_model_id, info.title)
            except Exception:
                log(traceback.format_exc())
                src = None
            return {"title": info.title, "artist": info.artist, "playing": playing, "src": src}

        return asyncio.run(go())
    except Exception:
        return None


def media_control(name):
    """Điều khiển trình phát qua Windows Media Session. Không giả lập phím media nên Windows không bật khung âm lượng."""
    try:
        from winrt.windows.media.control import (
            GlobalSystemMediaTransportControlsSessionManager as Manager,
        )

        async def go():
            s = pick_session(await Manager.request_async())
            if not s:
                return False
            fn = {"prev": s.try_skip_previous_async, "next": s.try_skip_next_async, "play": s.try_toggle_play_pause_async}[name]
            return bool(await fn())

        return asyncio.run(go())
    except Exception:
        log(traceback.format_exc())
        return False


TOAST_PS = r"""
$ErrorActionPreference='Stop'
[void][Windows.UI.Notifications.ToastNotificationManager,Windows.UI.Notifications,ContentType=WindowsRuntime]
[void][Windows.Data.Xml.Dom.XmlDocument,Windows.Data.Xml.Dom.XmlDocument,ContentType=WindowsRuntime]
$x=New-Object Windows.Data.Xml.Dom.XmlDocument
$x.LoadXml('<toast><visual><binding template="ToastGeneric"><text></text><text></text></binding></visual><audio silent="true"/></toast>')
$t=$x.GetElementsByTagName('text')
[void]$t.Item(0).AppendChild($x.CreateTextNode($env:RW_T))
[void]$t.Item(1).AppendChild($x.CreateTextNode($env:RW_B))
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('Focus Wind').Show([Windows.UI.Notifications.ToastNotification]::new($x))
"""
aumid_ready = False


def toast(title, body):
    """Thông báo Windows qua PowerShell + WinRT (có sẵn trong Windows, không cần cài thêm thư viện)."""
    global aumid_ready
    if not aumid_ready:  # đăng ký tên "Focus Wind" cho thông báo (chỉ ghi vào HKCU, không cần quyền admin)
        import winreg
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, r"Software\Classes\AppUserModelId\Focus Wind") as k:
            winreg.SetValueEx(k, "DisplayName", 0, winreg.REG_SZ, "Focus Wind")
        aumid_ready = True
    exe = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "WindowsPowerShell", "v1.0", "powershell.exe")
    cmd = base64.b64encode(TOAST_PS.encode("utf-16-le")).decode()
    r = subprocess.run([exe, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-EncodedCommand", cmd],
                       env=dict(os.environ, RW_T=str(title), RW_B=str(body)), stdin=subprocess.DEVNULL,
                       capture_output=True, text=True, timeout=30, creationflags=0x08000000)
    log("toast rc=%s %s" % (r.returncode, (r.stderr or "").strip()[:300]))


# --- Tự dò và cập nhật phiên bản (đọc latest.json ở UPDATE_URL trong version.py) ---
UPD_PREF = DATA / "update.json"  # {"auto": true/false}: bật = báo mọi bản mới; tắt = chỉ báo bản lớn (2.0, 3.0...)
_upd = None  # thông tin bản mới đang chờ người dùng quyết định

# Chạy trong cửa sổ chính: bản lớn = hộp thoại giữa màn hình, bản nhỏ = thẻ nhỏ ở góc phải dưới. Chữ chèn bằng textContent nên an toàn.
UPD_JS = r"""(function(i){
var o=document.getElementById('fw-upd');if(o)o.remove();
var d=document.createElement('div');d.id='fw-upd';
d.style.cssText='position:fixed;z-index:2147483647;font:14px Segoe UI,sans-serif;'+(i.major?'inset:0;background:rgba(0,0,0,.55);display:flex;align-items:center;justify-content:center':'right:16px;bottom:16px');
var c=document.createElement('div');
c.style.cssText='background:#2b2b2b;color:#fff;border-radius:12px;padding:18px 20px;width:min(380px,88vw);box-shadow:0 12px 40px rgba(0,0,0,.5)';
function el(t,css,txt){var e=document.createElement(t);e.style.cssText=css;if(txt)e.textContent=txt;return e}
c.appendChild(el('div','font-size:16px;font-weight:600','Có bản mới '+i.version));
if(i.notes)c.appendChild(el('div','margin-top:8px;opacity:.8;white-space:pre-wrap',i.notes));
var msg=el('div','margin-top:8px;color:#f88;display:none');c.appendChild(msg);
var row=el('div','display:flex;gap:8px;justify-content:flex-end;margin-top:14px');
var later=el('button','background:#444;color:#fff;border:0;border-radius:6px;padding:7px 14px;cursor:pointer','Để sau');
var go=el('button','background:#4cc2ff;color:#000;border:0;border-radius:6px;padding:7px 14px;cursor:pointer','Cập nhật ngay');
later.onclick=function(){d.remove()};
go.onclick=function(){go.disabled=true;go.textContent='Đang tải...';
pywebview.api.update_install().then(function(r){if(r&&r.ok)return;go.disabled=false;go.textContent='Thử lại';msg.textContent=(r&&r.msg)||'Không cập nhật được';msg.style.display='block'})};
row.append(later,go);c.appendChild(row);d.appendChild(c);document.body.appendChild(d)
})(__INFO__)"""


def _ver(s):
    """'1.2' -> (1, 2, 0); không đọc được thì trả (). Đủ 3 số để 1.2 == 1.2.0."""
    try:
        t = tuple(int(x) for x in str(s).strip().lstrip("vV").split("."))
    except ValueError:
        return ()
    return t + (0,) * (3 - len(t))


def update_auto_get():
    try:
        return bool(json.loads(UPD_PREF.read_text(encoding="utf-8")).get("auto", True))
    except Exception:
        return True


def update_fetch():
    """Đọc latest.json: {"version", "url", "notes", "sha256" (tùy chọn)}. Có bản mới hơn thì trả dict, không thì None."""
    if not UPDATE_URL:
        return None
    import urllib.request
    req = urllib.request.Request(UPDATE_URL, headers={"User-Agent": "FocusWind/" + APP_VERSION})
    with urllib.request.urlopen(req, timeout=10) as r:
        d = json.loads(r.read().decode("utf-8"))
    new, cur = _ver(d.get("version")), _ver(APP_VERSION)
    if not new or not cur or new <= cur:
        return None
    return {"version": str(d["version"]), "url": str(d.get("url", "")), "notes": str(d.get("notes", "")),
            "sha256": str(d.get("sha256", "")).lower(), "major": new[0] > cur[0]}


def update_run_installer(info):
    """Tải bộ cài về thư mục tạm, kiểm tra sha256 nếu có, rồi chạy bộ cài (cài đè lên bản đang dùng)."""
    import hashlib
    import tempfile
    import urllib.request
    if not info["url"].lower().startswith("https://"):
        raise ValueError("Link tải phải bắt đầu bằng https://")
    dest = Path(tempfile.gettempdir()) / ("FocusWind-Setup-%s.exe" % info["version"])
    req = urllib.request.Request(info["url"], headers={"User-Agent": "FocusWind/" + APP_VERSION})
    with urllib.request.urlopen(req, timeout=30) as r, dest.open("wb") as f:
        shutil.copyfileobj(r, f)
    if info["sha256"] and hashlib.sha256(dest.read_bytes()).hexdigest() != info["sha256"]:
        dest.unlink()
        raise ValueError("File tải về bị sai mã kiểm tra (sha256)")
    subprocess.Popen([str(dest), "/SILENT", "/NOCANCEL"], close_fds=True, creationflags=0x208)  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP


def update_notify(info):
    global _upd
    _upd = info
    title = "Focus Wind có bản mới " + info["version"]
    threading.Thread(target=safe(lambda: toast(title, "Mở Focus Wind để cập nhật")), daemon=True).start()
    if MAIN_WIN:
        MAIN_WIN.evaluate_js(UPD_JS.replace("__INFO__", json.dumps(info)))


def update_worker():
    seen = None
    time.sleep(10)  # chờ cửa sổ chính mở xong
    while True:
        try:
            info = update_fetch()
            if info and info["version"] != seen and (info["major"] or update_auto_get()):
                seen = info["version"]
                update_notify(info)
        except Exception:
            log("update:", traceback.format_exc())  # mất mạng / link lỗi: im lặng, 6 giờ sau thử lại
        time.sleep(6 * 3600)


class Api:
    """Các hàm JavaScript gọi được (tên bắt đầu bằng _ sẽ được ẩn)."""

    def __init__(self, kind):
        self._kind, self._w, self._h, self._main = kind, None, None, None
        self._cr = (True, "", "")  # (tối?, màu nền, màu chữ) của thanh tiêu đề; nhớ lại để áp dụng khi cửa sổ sẵn sàng

    def mode(self):
        return self._kind

    def load(self):
        try:
            return STATE.read_text(encoding="utf-8")
        except OSError:
            return "{}"

    def state_ver(self):  # Glint hỏi nhanh "file có đổi không" để khỏi đọc + parse cả state mỗi giây
        try:
            s = STATE.stat()
            return f"{s.st_mtime_ns}-{s.st_size}"
        except OSError:
            return ""

    def save(self, data):
        try:
            with _save_lock:
                tmp = STATE.with_suffix(".tmp")
                tmp.write_text(data, encoding="utf-8")
                os.replace(tmp, STATE)
        except Exception:
            log(traceback.format_exc())

    def title(self, text):
        if self._w:
            self._w.set_title(text)

    def media(self):  # Trình phát trong app thắng khi nó đang phát, hoặc khi nhạc ngoài không phát
        m, p = now_playing(), MP_NOW
        return p if p.get("title") and (p["playing"] or not (m and m["playing"])) else m

    def key(self, name):
        press_media(name)

    def mp_now(self, d):  # Trình phát báo bài đang có / đang phát, để Glint và phím tắt dùng chung
        global MP_NOW
        MP_NOW = {"title": str(d.get("title", ""))[:200], "artist": str(d.get("artist", ""))[:80], "src": "Focus Wind",
                  "playing": bool(d.get("playing")), "t": time.time()} if d and d.get("title") else {}

    def mp_pick(self, room=MP_MAX):  # nút "chọn file" của Trình phát
        p = ask_open_paths(self._h, "Chọn file nhạc")
        return mp_expand(p, int(room)) if p else []

    def mp_load(self):
        try:
            return MP_FILE.read_text(encoding="utf-8")
        except OSError:
            return "{}"

    def mp_save(self, data):
        try:
            with _save_lock:
                tmp = MP_FILE.with_suffix(".tmp")
                tmp.write_text(data, encoding="utf-8")
                os.replace(tmp, MP_FILE)
        except Exception:
            log(traceback.format_exc())

    def notify(self, title, body):  # thông báo Windows khi hết giờ Pomodoro
        threading.Thread(target=safe(lambda: toast(title, body)), daemon=True).start()

    def front(self):  # đưa app lên trước khi hết giờ Pomodoro
        if self._h:
            U.ShowWindow(self._h, 9)
            U.SetForegroundWindow(self._h)

    def chrome(self, dark=True, bg="", fg=""):  # thanh tiêu đề tối/sáng + (Win11) đúng màu app
        self._cr = (bool(dark), str(bg or ""), str(fg or ""))
        if self._h:
            paint_titlebar(self._h, *self._cr)

    # --- Dynamic Island ---
    def island(self, on):
        ISL.auto = False  # người dùng tự bật/tắt thì không để chế độ tự ẩn khi chơi game chen vào
        threading.Thread(target=safe(ISL.show if on else ISL.hide), daemon=True).start()

    def drag_start(self, seq=0):
        ISL.drag_start(seq)

    def drag_to(self, dx, dy):  # giữ lại cho tương thích bản cũ; việc kéo giờ do Python tự bám chuột
        pass

    def drag_end(self, seq=0):
        ISL.drag_end(seq)

    def pomo_toggle(self):  # nút bắt đầu/tạm dừng Pomodoro ở Dynamic Island -> chạy trong cửa sổ chính
        if self._main:
            self._main.evaluate_js("window.pomoToggle&&window.pomoToggle()")

    def pomo_skip(self):  # nút Bỏ qua ở Dynamic Island -> sang ngay phiên kế tiếp
        if self._main:
            self._main.evaluate_js("window.pomoSkip&&window.pomoSkip()")

    def pomo_reset(self):
        if self._main:
            self._main.evaluate_js("window.pomoReset&&window.pomoReset()")

    def note_add(self):  # nút Thêm ở Dynamic Island -> tạo ghi chú mới trong cửa sổ chính
        if self._main:
            self._main.evaluate_js("window.noteAdd&&window.noteAdd()")

    def go_home(self):  # nhấp đúp Glint: dí sát mép trên, giữa màn hình
        threading.Thread(target=safe(ISL.dock_top), daemon=True).start()

    def hotkeys_get(self):
        st = read_state()
        return {n: hk_string(n, st) for n in HK_ACTIONS}

    def hotkeys_suspend(self, on):
        """Giao diện đang ghi phím mới: gỡ tạm phím tắt để bấm trúng phím đang dùng cũng không kích hoạt chức năng."""
        global _hk_suspended
        _hk_suspended = bool(on)
        hotkey_apply()
        return True

    def hotkey_set(self, action, key_str=""):
        """Đặt phím tắt cho một chức năng; chuỗi rỗng = tắt phím tắt. Trả về {ok, msg, text, hks}."""
        global _hk_suspended
        if action not in HK_ACTIONS:
            return {"ok": False, "msg": "Chức năng không tồn tại"}
        key_str = (key_str or "").strip()
        txt = ""
        if key_str:
            hk = parse_hotkey(key_str)
            if not hk or not (hk["mod"] or 0x70 <= hk["vk"] <= 0x87):
                return {"ok": False, "msg": "Cần kèm Ctrl, Alt, Shift hoặc Win (hoặc dùng phím F1-F24)"}
            txt = hotkey_to_string(hk)
            st = read_state()
            for other in HK_ACTIONS:
                if other != action and hk_string(other, st).lower() == txt.lower():
                    return {"ok": False, "msg": "Phím này đã dùng cho chức năng khác"}

        def write(val):
            with _save_lock:
                st = read_state()
                hks = dict(st.get("hks") or {})
                if val is None:
                    hks.pop(action, None)
                else:
                    hks[action] = val
                st["hks"] = hks
                tmp = STATE.with_suffix(".tmp")
                tmp.write_text(json.dumps(st, ensure_ascii=False), encoding="utf-8")
                os.replace(tmp, STATE)
                return hks

        old = (read_state().get("hks") or {}).get(action)

        def apply_and_wait():
            gen = _hk_gen
            hotkey_apply()
            for _ in range(60):  # tối đa ~1.5 giây
                if _hk_gen != gen:
                    break
                time.sleep(0.025)

        _hk_suspended = False
        hks = write(txt)
        apply_and_wait()
        if txt and not _hk_ok.get(action):  # Windows từ chối (phím đang bị app khác chiếm): trả lại như cũ
            hks = write(old)
            apply_and_wait()
            return {"ok": False, "msg": "Phím này đang bị ứng dụng khác dùng, hãy chọn tổ hợp khác", "hks": hks}
        return {"ok": True, "text": txt, "hks": hks}

    # --- Khởi động cùng Windows / shortcut ---
    def autostart_get(self):
        try:
            return autostart_get()
        except Exception:
            log(traceback.format_exc())
            return False

    def autostart_set(self, on):
        try:
            autostart_set(bool(on))
            return True
        except Exception:
            log(traceback.format_exc())
            return False

    def make_shortcut(self):
        """Mở hộp thoại 'Lưu thành...' để người dùng chọn nơi lưu, rồi tạo shortcut tại đó."""
        try:
            path = ask_save_path("Focus Wind.lnk", self._h, "Tạo shortcut Focus Wind",
                                 filt="Shortcut (*.lnk)\0*.lnk\0\0", defext="lnk", initdir=desktop_dir())
            if path is None:
                return {"ok": False, "msg": "Đang mở hộp thoại khác"}
            if not path:
                return {"ok": False, "msg": "Đã hủy"}
            if not path.lower().endswith(".lnk"):
                path += ".lnk"
            create_lnk(path)
            return {"ok": True, "path": path}
        except Exception as e:
            log(traceback.format_exc())
            return {"ok": False, "msg": str(e)}

    # --- Cập nhật phiên bản ---
    def update_auto_get(self):
        return update_auto_get()

    def update_auto_set(self, on):
        UPD_PREF.write_text(json.dumps({"auto": bool(on)}), encoding="utf-8")
        return True

    def update_install(self):
        """Nút 'Cập nhật ngay': tải bộ cài, chạy nó rồi đóng app để bộ cài ghi đè file."""
        try:
            if not _upd:
                return {"ok": False, "msg": "Chưa có bản mới"}
            update_run_installer(_upd)
        except Exception as e:
            log(traceback.format_exc())
            return {"ok": False, "msg": str(e)}
        if MAIN_WIN:
            MAIN_WIN.destroy()
        return {"ok": True}

    def export_notes(self, ids=None):
        """Xuất các ghi chú được chọn (ids) ra file .md; không truyền ids thì xuất tất cả."""
        try:
            st = read_state()
            notes = st.get("notes", [])
            if ids is not None:
                want = {int(i) for i in ids if isinstance(i, (int, float))}
                notes = [n for n in notes if isinstance(n.get("id"), (int, float)) and int(n["id"]) in want]
            if not notes:
                return {"ok": False, "msg": "Không có ghi chú"}
            lines = ["# Ghi chú Focus Wind\n"]
            for n in sorted(notes, key=lambda x: 1 if x.get("s") else 0, reverse=True):
                star = "⭐ " if n.get("s") else ""
                title = n.get("t") or "Không tiêu đề"
                body = n.get("b", "")
                lines.append(f"## {star}{title}\n")
                if body:
                    lines.append(body + "\n")
                lines.append("---\n")
            md = "\n".join(lines)

            path = ask_save_path("FocusWind_Notes.md", self._h, "Xuất ghi chú ra Markdown")
            if path is None:
                return {"ok": False, "msg": "Đang mở hộp thoại khác"}
            if not path:
                return {"ok": False, "msg": "Đã hủy"}
            if not path.lower().endswith(".md"):
                path += ".md"
            Path(path).write_text(md, encoding="utf-8")
            return {"ok": True, "path": path}
        except Exception as e:
            log(traceback.format_exc())
            return {"ok": False, "msg": str(e)}


def game_tick():
    """Gọi mỗi ~1.5 giây: tự ẩn Island khi có game / ứng dụng toàn màn hình (nếu bật trong Cài đặt), thoát thì tự hiện lại."""
    st = state_flags()
    if not st.get("isl", True):
        ISL.auto = False
        return
    busy = bool(st.get("ag", True)) and fullscreen_busy()
    if busy and ISL.vis:
        ISL.auto = True
        ISL.hide(fast=True)
    elif not busy and ISL.auto:
        ISL.auto = False
        ISL.show()


def watch_games():
    while True:
        time.sleep(1.5)
        try:
            game_tick()
        except Exception:
            log(traceback.format_exc())


class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [("dwSize", wt.DWORD), ("cntUsage", wt.DWORD), ("th32ProcessID", wt.DWORD),
                ("th32DefaultHeapID", ctypes.c_size_t), ("th32ModuleID", wt.DWORD), ("cntThreads", wt.DWORD),
                ("th32ParentProcessID", wt.DWORD), ("pcPriClassBase", ctypes.c_long), ("dwFlags", wt.DWORD),
                ("szExeFile", wt.WCHAR * 260)]


K.CreateToolhelp32Snapshot.argtypes, K.CreateToolhelp32Snapshot.restype = [wt.DWORD, wt.DWORD], wt.HANDLE
K.Process32FirstW.argtypes = K.Process32NextW.argtypes = [wt.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
K.Process32FirstW.restype = K.Process32NextW.restype = wt.BOOL
K.OpenProcess.argtypes, K.OpenProcess.restype = [wt.DWORD, wt.BOOL, wt.DWORD], wt.HANDLE
K.K32EmptyWorkingSet.argtypes = [wt.HANDLE]
K.CloseHandle.argtypes = [wt.HANDLE]
K.GetCurrentProcessId.restype = wt.DWORD


def trim_memory():
    """Bảo Windows thu hồi RAM đang giữ của Focus Wind và các tiến trình WebView2 con (trang nhớ tự nạp lại khi cần dùng)."""
    snap = K.CreateToolhelp32Snapshot(0x2, 0)  # TH32CS_SNAPPROCESS
    if not snap or snap == ctypes.c_void_p(-1).value:
        return
    parent, e = {}, PROCESSENTRY32W()
    e.dwSize = ctypes.sizeof(e)
    ok = K.Process32FirstW(snap, ctypes.byref(e))
    while ok:
        parent[e.th32ProcessID] = e.th32ParentProcessID
        ok = K.Process32NextW(snap, ctypes.byref(e))
    K.CloseHandle(snap)
    me = K.GetCurrentProcessId()

    def mine(p):  # p là chính app hoặc tiến trình con cháu của app
        for _ in range(8):
            if p == me:
                return True
            p = parent.get(p)
            if not p:
                return False
        return False

    for pid in [p for p in parent if mine(p)]:
        h = K.OpenProcess(0x0500, False, pid)  # PROCESS_QUERY_INFORMATION | PROCESS_SET_QUOTA
        if h:
            K.K32EmptyWorkingSet(h)
            K.CloseHandle(h)


def trim_worker(main_api):
    """Cửa sổ chính vừa bị thu nhỏ (và đang bật Tối ưu RAM) -> chờ 3 giây cho trang yên rồi thu hồi RAM, mỗi lần thu nhỏ làm một lần."""
    was = False
    while True:
        time.sleep(1.5)
        try:
            mini = bool(main_api._h) and bool(U.IsIconic(main_api._h))
            if mini and not was and read_state().get("ram"):
                time.sleep(3)
                if U.IsIconic(main_api._h):
                    trim_memory()
            was = mini
        except Exception:
            log(traceback.format_exc())


@safe
def mp_on_drop(e):
    try:
        fs = e["dataTransfer"]["files"]
        ps = mp_expand([f["pywebviewFullPath"] for f in fs if f.get("pywebviewFullPath")])
        if ps and MAIN_WIN:
            MAIN_WIN.evaluate_js(f"window.mpAdd&&window.mpAdd({json.dumps(ps)})")
    except Exception:
        log(traceback.format_exc())


def mp_bind_drop(win):
    """Thả file/thư mục nhạc vào cửa sổ -> Trình phát. Cần pywebview >= 5; bản cũ hơn thì còn nút chọn file."""
    try:
        from webview.dom import DOMEventHandler
        win.events.loaded.wait(20)
        win.dom.document.events.drop += DOMEventHandler(mp_on_drop, False, False)  # giao diện tự chặn mặc định khi kéo file
    except Exception:
        log("thả file không dùng được:", traceback.format_exc())


def boot(main_api, island_win):
    main_api._h = find("Focus Wind")
    main_api.chrome(*main_api._cr)
    island_win.events.loaded.wait(20)
    main_api.chrome(*main_api._cr)  # áp dụng lại khi cửa sổ đã hiện hẳn (lần đầu đôi khi Windows vẽ đè lại màu trắng)
    threading.Thread(target=hotkey_worker, daemon=True).start()  # phím tắt hoạt động cả khi Glint lỗi
    threading.Thread(target=mp_bind_drop, args=(main_api._w,), daemon=True).start()
    threading.Thread(target=trim_worker, args=(main_api,), daemon=True).start()
    threading.Thread(target=update_worker, daemon=True).start()
    if not ISL.setup():
        return
    st = read_state()
    if st.get("isl", True):
        if st.get("ag", True) and fullscreen_busy():
            ISL.auto = True  # đang chơi game lúc mở app: chưa hiện, thoát game sẽ tự hiện
        else:
            ISL.show()
    threading.Thread(target=watch_games, daemon=True).start()


def run():
    main_api, isl_api = Api("main"), Api("island")
    base = (ROOT / "ui" / "index.html").as_uri()
    main = webview.create_window("Focus Wind", base + "#main", js_api=main_api, width=1100, height=720,
                                 min_size=(820, 560), background_color="#202020")
    opts = dict(js_api=isl_api, width=ISL_W, height=ISL_H, min_size=(300, 56), x=-3000, y=-3000, frameless=True,
                on_top=True, easy_drag=False, resizable=False, focus=False, background_color="#111111")
    try:
        island = webview.create_window("Glint", base + "#island", shadow=False, **opts)
    except TypeError:  # bản pywebview cũ không có tham số shadow
        island = webview.create_window("Glint", base + "#island", **opts)
    global MAIN_WIN
    MAIN_WIN = main
    main_api._w, isl_api._w = main, island
    isl_api._main = main
    main.events.closed += island.destroy
    ico = ROOT / "icon.ico"
    webview.start(boot, (main_api, island), icon=str(ico) if ico.is_file() else None)


if __name__ == "__main__":
    run()
