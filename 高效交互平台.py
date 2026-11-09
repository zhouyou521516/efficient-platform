# -*- coding: utf-8 -*-
"""
高效交互平台 Lite —— 手机投屏 / 多开 / 文件互传  一体化图形界面

启动方式：双击同级目录的「启动.bat」
依赖：Python 标准库（tkinter）+ 同目录下的 scrcpy（内置 adb）
"""

import base64
import os
import queue
import re
import subprocess
import threading
import time
import tkinter as tk
import xml.etree.ElementTree as ET
from tkinter import ttk, filedialog

# ---------------------------------------------------------------- 高 DPI 适配
try:
    import ctypes
    ctypes.windll.shcore.SetProcessDpiAwareness(1)
except Exception:
    pass

# ---------------------------------------------------------------- 路径
BASE = os.path.dirname(os.path.abspath(__file__))
SCRCPY_DIR = os.path.join(BASE, "scrcpy")
ADB = os.path.join(SCRCPY_DIR, "adb.exe")
SCRCPY = os.path.join(SCRCPY_DIR, "scrcpy.exe")

CREATE_NO_WINDOW = 0x08000000
CREATE_NEW_CONSOLE = 0x00000010
QUIET = {"creationflags": CREATE_NO_WINDOW}

# 文件页实时刷新与拖拽参数
WATCH_MS = 1500          # 两边目录的轮询基准间隔（毫秒）；只在文件页可见时跑
WATCH_MS_MAX = 6000      # 长时间无变化后的最长间隔：静止时不再持续创建 adb 进程
SERIAL_TTL = 5.0         # 设备序列号缓存秒数，避免每次轮询都跑一遍 adb devices
DRAG_THRESHOLD = 6       # 位移超过这个像素才算拖拽，避免普通点击被误判

# 文件页缩略图网格
THUMB_PX   = 96          # 缩略图最大边长（像素）
TILE_W     = 118         # 单个格子宽度（配合常见分栏宽度刚好放 3 列）
TILE_H     = 146         # 单个格子高度（缩略图 + 两行文件名）
MAX_TILES  = 600         # 单目录最多渲染格子数，防超大目录把界面拖死
THUMB_CACHE = os.path.join(os.environ.get("TEMP") or os.path.expanduser("~"),
                           "efficient_platform_thumbs")

# 能显示真实缩略图的扩展名（GDI+ 自带解码器全部覆盖）
IMG_EXT = {".jpg", ".jpeg", ".png", ".bmp", ".gif", ".tif", ".tiff", ".webp", ".heic", ".jfif"}
VID_EXT = {".mp4", ".mov", ".avi", ".mkv", ".3gp", ".wmv", ".flv", ".m4v"}
AUD_EXT = {".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg", ".amr", ".wma"}
DOC_EXT = {".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
           ".txt", ".md", ".csv", ".rtf", ".json", ".xml"}
ZIP_EXT = {".zip", ".rar", ".7z", ".tar", ".gz", ".apk"}

# 手机侧「位置」：和手机自带文件管理器里的入口一一对应，点一下右侧就跳过去。
# path 必须真实存在；启动时会用 adb 探一遍，不存在的入口自动灰掉。
PHONE_HOME = "/sdcard/Download/"
PHONE_PLACES = [
    ("下载与接收", "/sdcard/Download"),
    ("文档",      "/sdcard/Documents"),
    ("相册",      "/sdcard/DCIM/Camera"),
    ("图片",      "/sdcard/Pictures"),
    ("视频",      "/sdcard/Movies"),
    ("音乐",      "/sdcard/Music"),
    ("内部存储",   "/sdcard"),
]

# ---------------------------------------------------------------- 配色
BG        = "#f4f6f9"
SIDEBAR   = "#ffffff"
CARD      = "#ffffff"
BORDER    = "#e6e9ef"
TEXT      = "#1f2430"
MUTED     = "#8a93a6"
ACCENT    = "#2f6bff"
ACCENT_D  = "#2456d6"
OK        = "#12a06a"
WARN      = "#d4791a"
ERR       = "#d94141"

F_TITLE   = ("Microsoft YaHei UI", 16, "bold")
F_SUB     = ("Microsoft YaHei UI", 9)
F_ITEM    = ("Microsoft YaHei UI", 11)
F_BODY    = ("Microsoft YaHei UI", 10)
F_SMALL   = ("Microsoft YaHei UI", 9)
F_MONO    = ("Consolas", 9)


# ---------------------------------------------------------------- 工具函数
def run(cmd, timeout=120, cwd=None):
    try:
        p = subprocess.run(cmd, capture_output=True, timeout=timeout, cwd=cwd, **QUIET)
        return p.returncode, (p.stdout + p.stderr).decode("utf-8", "replace")
    except subprocess.TimeoutExpired:
        return 1, "执行超时"
    except Exception as e:
        return 1, str(e)


def adb(args, timeout=120):
    return run([ADB] + args, timeout=timeout)


def shq(path):
    """把任意路径安全地塞进手机端 sh 命令。

    为什么不用引号拼字符串：`adb push` 在 Windows 上会把非 ASCII 文件名截断
    （实测 周游开题报告_讨论稿_v3_20260922.docx -> 周游开题报告_讨论稿_v3），
    而 `adb shell` 里的中文是好的。所以文件先以纯 ASCII 临时名推上去，再由
    手机端自己改名——改名用的路径必须原样送达，base64 是唯一不会被中途
    改写的方式（设备端 toybox/base64 已验证可用）。
    """
    b64 = base64.b64encode(str(path).encode("utf-8")).decode("ascii")
    return '"$(echo ' + b64 + ' | base64 -d)"'


# ---------------------------------------------------------------- 缩略图
# 用 Windows 自带的 gdiplus.dll（ctypes 扁平 API），不需要 PIL。
# 单张 5~50ms，支持 jpg/png/bmp/gif/tiff/webp 等 GDI+ 自带解码格式。
_gdi_lock = threading.Lock()
_gdi_state = {"token": None, "ok": None}


def _gdi_ready():
    """延迟初始化 GDI+（进程内只做一次）。"""
    with _gdi_lock:
        if _gdi_state["ok"] is not None:
            return _gdi_state["ok"]
        if os.name != "nt":
            _gdi_state["ok"] = False
            return False
        try:
            import ctypes
            gp = ctypes.WinDLL("gdiplus.dll")

            class StartupInput(ctypes.Structure):
                _fields_ = [("GdiplusVersion", ctypes.c_uint32),
                            ("DebugEventCallback", ctypes.c_void_p),
                            ("SuppressBackgroundThread", ctypes.c_bool),
                            ("SuppressExternalCodecs", ctypes.c_bool)]

            gp.GdipCreateBitmapFromFile.argtypes = [ctypes.c_wchar_p,
                                                    ctypes.POINTER(ctypes.c_void_p)]
            gp.GdipCreateBitmapFromFile.restype = ctypes.c_int
            gp.GdipGetImageThumbnail.argtypes = [
                ctypes.c_void_p, ctypes.c_uint, ctypes.c_uint,
                ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p, ctypes.c_void_p]
            gp.GdipGetImageThumbnail.restype = ctypes.c_int
            gp.GdipSaveImageToFile.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p,
                                               ctypes.c_void_p, ctypes.c_void_p]
            gp.GdipSaveImageToFile.restype = ctypes.c_int
            gp.GdipDisposeImage.argtypes = [ctypes.c_void_p]
            gp.GdipDisposeImage.restype = ctypes.c_int

            token = ctypes.c_ulong()
            si = StartupInput(1, None, False, False)
            if gp.GdiplusStartup(ctypes.byref(token), ctypes.byref(si), None) != 0:
                _gdi_state["ok"] = False
                return False
            _gdi_state.update(token=token, ok=True, gp=gp)
            return True
        except Exception:
            _gdi_state["ok"] = False
            return False


def _png_clsid():
    class GUID(ctypes.Structure):
        _fields_ = [("Data1", ctypes.c_uint32), ("Data2", ctypes.c_uint16),
                    ("Data3", ctypes.c_uint16), ("Data4", ctypes.c_ubyte * 8)]
    g = GUID()
    ctypes.windll.ole32.CLSIDFromString(
        ctypes.c_wchar_p("{557CF406-1A04-11D3-9A73-0000F81EF32E}"), ctypes.byref(g))
    return g


def make_thumb(src, dst, px=THUMB_PX):
    """把 src 生成 px 大小的缩略图 PNG 写到 dst。成功返回 True。

    GDI+ 的 GdipGetImageThumbnail 会保持长宽比，不会把图拉变形。
    """
    if not _gdi_ready():
        return False
    gp = _gdi_state["gp"]
    bmp = ctypes.c_void_p()
    try:
        if gp.GdipCreateBitmapFromFile(str(src), ctypes.byref(bmp)) != 0 or not bmp:
            return False
        thumb = ctypes.c_void_p()
        if gp.GdipGetImageThumbnail(bmp, px, px, ctypes.byref(thumb), None, None) != 0 or not thumb:
            return False
        try:
            clsid = _png_clsid()
            ok = gp.GdipSaveImageToFile(thumb, str(dst), ctypes.byref(clsid), None) == 0
        finally:
            gp.GdipDisposeImage(thumb)
        return ok and os.path.exists(dst) and os.path.getsize(dst) > 0
    except Exception:
        return False
    finally:
        if bmp:
            try:
                gp.GdipDisposeImage(bmp)
            except Exception:
                pass


def thumb_cache_path(key):
    """缓存文件名用 hash：避免中文/超长文件名反复触发路径长度限制。

    尺寸也进 hash：改了格子大小之后旧图自动失效，不会出现小格子塞大图。
    """
    import hashlib
    h = hashlib.md5(f"{key}|{THUMB_PX}".encode("utf-8", "replace")).hexdigest()[:24]
    return os.path.join(THUMB_CACHE, h + ".png")


ROW_H = 30                     # 列表视图单行高度
KIND_CHOICES = [("全部", ""), ("文档", "doc"), ("图片", "img"),
                ("视频", "vid"), ("音频", "aud"), ("压缩包", "zip")]
SORT_CHOICES = [("名称", "name"), ("最新", "new"), ("大小", "size")]


def file_kind(name, isdir):
    """返回 (类型标识, 角标字形, 角标颜色)，用于非图片文件显示形象图标。"""
    if isdir:
        return "dir", "📁", "#e8a33d"
    ext = os.path.splitext(name)[1].lower()
    if ext in IMG_EXT:
        return "img", "🖼", "#3a86ff"
    if ext in VID_EXT:
        return "vid", "🎬", "#8b5cf6"
    if ext in AUD_EXT:
        return "aud", "🎵", "#12a06a"
    if ext in DOC_EXT:
        return "doc", "📄", "#4b7bec"
    if ext in ZIP_EXT:
        return "zip", "📦", "#b07d3a"
    return "file", "📄", "#8a93a6"


_SERIAL_CACHE = {"value": "", "ts": 0.0}


class AdbShellSession:
    """常驻 adb shell 会话。

    每次 `adb shell ...` 都要新建一个 adb.exe 进程（实测约 170ms），文件页
    轮询每几秒就要列一次目录，反复建进程既烧 CPU，又和投屏抢 adb 通道。
    常驻会话把单次命令压到几毫秒：只在启动时付一次代价。
    """

    def __init__(self, serial):
        self.serial = serial
        self._lock = threading.Lock()
        self._proc = None
        self._q = None
        self._seq = 0
        self._start()

    def _start(self):
        try:
            self._proc = subprocess.Popen(
                [ADB, "-s", self.serial, "shell"],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, **QUIET)
        except OSError:
            self._proc = None
            return False
        q = queue.Queue()
        proc = self._proc

        def reader():
            try:
                for line in iter(proc.stdout.readline, b""):
                    q.put(line)
            except Exception:
                pass
            q.put(None)                      # 进程结束

        threading.Thread(target=reader, daemon=True).start()
        self._q = q
        return True

    def alive(self):
        return self._proc is not None and self._proc.poll() is None

    def close(self):
        p, self._proc = self._proc, None
        if p is not None:
            try:
                p.kill()
            except Exception:
                pass

    def run(self, cmd, timeout=8.0):
        if not self.alive():
            self.close()
            if not self._start():
                return None
        with self._lock:
            self._seq += 1
            marker = f"@@E{self._seq}@@"
            try:
                self._proc.stdin.write((cmd + "\necho " + marker + "\n").encode("utf-8"))
                self._proc.stdin.flush()
            except (OSError, ValueError):
                self.close()
                return None
            buf = []
            deadline = time.time() + timeout
            while True:
                left = deadline - time.time()
                if left <= 0:
                    self.close()             # 卡死就丢掉会话，下次重建
                    return None
                try:
                    line = self._q.get(timeout=left)
                except queue.Empty:
                    self.close()
                    return None
                if line is None:             # 设备断开
                    self.close()
                    return None
                s = line.decode("utf-8", "replace")
                if marker in s:
                    return "".join(buf)
                buf.append(s)


_SHELLS = {}
_SHELL_LOCK = threading.Lock()


def adb_shell(serial, cmd, timeout=8.0):
    """走常驻会话执行一条 shell 命令；不可用时返回 None，由调用方降级。"""
    if os.environ.get("NO_PERSIST_SHELL"):
        return None
    with _SHELL_LOCK:
        # 换了设备就丢掉旧会话，别留着白占一个 adb 进程
        for k in [k for k in _SHELLS if k != serial]:
            _SHELLS.pop(k).close()
        sess = _SHELLS.get(serial)
        if sess is None or not sess.alive():
            sess = AdbShellSession(serial)
            _SHELLS[serial] = sess
    return sess.run(cmd, timeout=timeout)


def drop_adb_shell():
    """设备切换或出错时丢弃所有常驻会话。"""
    with _SHELL_LOCK:
        for sess in _SHELLS.values():
            sess.close()
        _SHELLS.clear()


def get_serial(fresh=False):
    """取当前设备序列号（带缓存）。

    文件页轮询每 1.5 秒就要调一次，而 `adb devices` 每次都要新建一个
    adb.exe 进程（实测约 110ms）——不缓存的话一半的轮询开销纯属浪费。
    只缓存成功结果：没设备时必须实时重查，否则刚插线会被误判为「未连接」。
    """
    now = time.time()
    if not fresh and _SERIAL_CACHE["value"] and now - _SERIAL_CACHE["ts"] < SERIAL_TTL:
        return _SERIAL_CACHE["value"]
    rc, out = adb(["devices"])
    for line in out.splitlines():
        line = line.strip()
        if line.endswith("\tdevice") or line.endswith(" device"):
            sn = line.split()[0]
            _SERIAL_CACHE["value"] = sn
            _SERIAL_CACHE["ts"] = now
            return sn
    _SERIAL_CACHE["value"] = ""
    return None


TOOLS_DIR = os.path.join(BASE, "工具")
USB_FIX_BAT = os.path.join(TOOLS_DIR, "USB诊断修复.bat")


def adb_device_states():
    """列出 adb 已知的全部设备及状态（device / unauthorized / offline）。

    只取 `device` 会丢掉「已插上但未授权」这种中间态——那正是最容易误判成
    「换了手机就认不出」的情形，所以状态要原样带出来。
    """
    rc, out = adb(["devices"])
    states = []
    for line in (out or "").splitlines()[1:]:
        parts = line.strip().split()
        if len(parts) >= 2:
            states.append((parts[0], parts[1]))
    return states


# 查 USB 层：按接口协议特征（SubClass_42 & Prot_01）找 ADB 接口，不看厂商 ID。
# 这是所有 Android 设备 ADB 接口的标准描述符，换任何机型都适用——
# 老版本按 VID_12D1&PID_107E 写死，换台手机就失效，是典型的把个案当规则。
_USB_DIAG_PS = r"""
$ErrorActionPreference='SilentlyContinue'
$r=@()
foreach($d in (Get-PnpDevice -PresentOnly | Where-Object { $_.InstanceId -like 'USB\VID_*&MI_*' })){
  $p=Get-PnpDeviceProperty -InstanceId $d.InstanceId
  $cid=($p|Where-Object KeyName -eq 'DEVPKEY_Device_CompatibleIds').Data
  if($cid -and (($cid -join ' ') -match 'SubClass_42')){
    $pc=($p|Where-Object KeyName -eq 'DEVPKEY_Device_ProblemCode').Data
    $inst=($d.InstanceId -split '\\')[-1]
    $has=0
    foreach($c in (Get-ChildItem 'HKLM:\SYSTEM\CurrentControlSet\Control\DeviceClasses')){
      foreach($s in (Get-ChildItem $c.PSPath)){
        if($s.PSChildName -like ('*'+$inst+'*') -and $s.PSChildName -match 'f72fe0d4'){$has=1}
      }
    }
    $r+=(""+$pc+'|'+$has)
  }
}
if($r.Count -eq 0){'NONE'}else{$r -join ';'}
"""


def usb_diagnose():
    """没连上时判断卡在哪一层。返回 (代码, 界面提示, 详细说明)。

    链路共四层，从上往下依次排查：线材与开关 → 接口枚举 → 驱动注册 → adb 授权。
    换机型最容易踩的是第三层：接口枚举成功了（设备管理器看着一切正常），
    但 GUID 没注册，adb 扫描结果就是空的，看起来和「没插」一模一样。
    """
    try:
        p = subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
             "-Command", _USB_DIAG_PS],
            capture_output=True, timeout=15, **QUIET)
        txt = (p.stdout + p.stderr).decode("utf-8", "replace").strip()
    except Exception as exc:
        return "unclear", "诊断未完成", f"查询 USB 设备失败：{exc}"

    if "NONE" in txt or not txt:
        return ("no-interface",
                "电脑没看到调试接口 · 换线试试",
                "电脑完全没看到 ADB 接口。依次确认：①换一根确定能传数据的线；"
                "②手机「USB 调试」已开；③通知栏 USB 用途选「传输文件」而不是「仅充电」。")

    items = [it for it in txt.split(";") if "|" in it]
    if not items:
        return "unclear", "诊断结果无法解析", txt[:200]

    # 同时插多台手机时，任一接口注册好了就该正常显示，不能被另一台带偏
    problems, any_guid = [], False
    for it in items:
        ps, gs = (it.split("|") + ["-1", "0"])[:2]
        try:
            problems.append(int(ps))
        except ValueError:
            problems.append(-1)
        if gs == "1":
            any_guid = True

    # 驱动装不上 / 加载失败：重写 GUID、重装接口都有机会救回来
    # 10 = 无法启动，28 = 未安装驱动，31 = 驱动无法加载
    for pc in problems:
        if pc in (10, 28, 31):
            return ("driver-error",
                    "接口驱动异常 · 点「修复 USB 接口」",
                    f"ADB 接口存在，但驱动加载失败（问题码 {pc}），常见原因是注册表里残留了"
                    "冲突的 DeviceInterfaceGUIDs。点「修复 USB 接口」，允许管理员权限后按提示走。")

    # 43 = 端口复位失败：接口能读到，但设备复位后没站起来。
    # 这是电气层问题，写 GUID 完全无用，别把人往修复按钮上引。
    if 43 in problems:
        return ("link-error",
                "接口复位失败 · 换线或换口",
                "电脑能读到 ADB 接口描述符，但端口复位失败（问题码 43），说明信号链路不稳。"
                "换一根确定能传数据的短线，直插机身的 USB 口——不要经过扩展坞、延长线或"
                "前面板 hub；插好等 10 秒再点「重新检测」。")

    if any_guid:
        return ("interface-ok",
                "接口正常 · 等手机确认授权",
                "Windows 侧完全正常，adb 也能扫到接口，但设备没有应答。"
                "请在手机屏幕上点「允许 USB 调试」并勾选「始终允许」；"
                "如果没弹窗，把 USB 调试关掉再打开一次。")

    return ("not-registered",
            "接口未注册 · 点「修复 USB 接口」",
            "Windows 认到了手机的 ADB 接口，但该接口没有注册 ADB 设备接口 GUID，"
            "所以 adb 的扫描结果是空的。点「修复 USB 接口」，允许管理员权限，跑完点「重新检测」。")


def human_size(n):
    try:
        n = float(n)
    except Exception:
        return ""
    for u in ("B", "KB", "MB", "GB"):
        if n < 1024 or u == "GB":
            return f"{int(n)} {u}" if u == "B" else f"{n:.1f} {u}"
        n /= 1024.0
    return ""


# ---------------------------------------------------------------- 自定义控件
class FlatButton(tk.Label):
    """扁平按钮：primary 实心，secondary 描边"""

    def __init__(self, master, text, command, primary=False, width=None, padx=18, pady=9):
        self.primary = primary
        self.command = command
        self.bg_normal = ACCENT if primary else "#ffffff"
        self.bg_hover = ACCENT_D if primary else "#f0f4ff"
        self.fg = "#ffffff" if primary else TEXT
        super().__init__(master, text=text, bg=self.bg_normal, fg=self.fg,
                         font=("Microsoft YaHei UI", 10, "bold" if primary else "normal"),
                         padx=padx, pady=pady, cursor="hand2",
                         highlightthickness=0 if primary else 1,
                         highlightbackground=BORDER)
        if width:
            self.configure(width=width)
        self.bind("<Enter>", lambda e: self.configure(bg=self.bg_hover))
        self.bind("<Leave>", lambda e: self.configure(bg=self.bg_normal))
        self.bind("<Button-1>", lambda e: self.command())

    def enable(self, on=True):
        if on:
            self.configure(bg=self.bg_normal, fg=self.fg, cursor="hand2")
        else:
            self.configure(bg="#eef0f4", fg="#b3bac7", cursor="arrow")


class NavItem(tk.Frame):
    """左侧导航项"""

    def __init__(self, master, text, hint, command):
        super().__init__(master, bg=SIDEBAR, cursor="hand2")
        self.command = command
        self.active = False
        self.bar = tk.Frame(self, bg=SIDEBAR, width=3)
        self.bar.pack(side="left", fill="y")
        inner = tk.Frame(self, bg=SIDEBAR)
        inner.pack(side="left", fill="both", expand=True, padx=(13, 10), pady=8)
        self.lbl = tk.Label(inner, text=text, bg=SIDEBAR, fg=TEXT,
                            font=("Microsoft YaHei UI", 11), anchor="w")
        self.lbl.pack(fill="x")
        self.hint = tk.Label(inner, text=hint, bg=SIDEBAR, fg=MUTED,
                             font=F_SMALL, anchor="w")
        self.hint.pack(fill="x")
        for w in (self, inner, self.lbl, self.hint):
            w.bind("<Button-1>", lambda e: self.command())
            w.bind("<Enter>", lambda e: self._hover(True))
            w.bind("<Leave>", lambda e: self._hover(False))

    def _paint(self, bg):
        self.configure(bg=bg)
        for w in self.winfo_children():
            w.configure(bg=bg)
            for c in w.winfo_children():
                c.configure(bg=bg)

    def _hover(self, on):
        if not self.active:
            self._paint("#f7f9fd" if on else SIDEBAR)

    def set_active(self, on):
        self.active = on
        bg = "#eef3ff" if on else SIDEBAR
        self._paint(bg)
        self.bar.configure(bg=ACCENT if on else bg)
        self.lbl.configure(fg=ACCENT if on else TEXT,
                           font=("Microsoft YaHei UI", 11, "bold" if on else "normal"))


class FileGrid(tk.Frame):
    """缩略图网格。

    为什么不用列表：文件页的核心问题是「哪张图是我要的那张」——只看名字，
    一堆 `Image_1762169035313.jpg` 完全分不出内容。这里把图片的真实缩略图
    画出来，文件名放下面一行，一眼能认。

    非图片文件画成带扩展名的色块（PDF / MP4 / XLSX…），既省解码又比通用
    图标更容易区分。文件夹用黄色块 + 名字。
    """

    def __init__(self, master, side, on_open, on_need_thumb=None, on_hint=None):
        super().__init__(master, bg=CARD, highlightthickness=0)
        self.side = side
        self.on_open = on_open                  # 双击：(name, isdir)
        self.on_need_thumb = on_need_thumb      # 需要缩略图：(name, size)
        self.on_hint = on_hint                  # 悬停说明：(text)

        self.canvas = tk.Canvas(self, bg=CARD, highlightthickness=0, bd=0,
                                yscrollincrement=TILE_H // 3)
        self.sb = ttk.Scrollbar(self, orient="vertical", command=self._on_scroll)
        self.canvas.configure(yscrollcommand=self.sb.set)
        self.canvas.grid(row=0, column=0, sticky="nsew")
        self.sb.grid(row=0, column=1, sticky="ns")
        self.rowconfigure(0, weight=1)
        self.columnconfigure(0, weight=1)

        self.view = "grid"            # grid=缩略图 / list=列表
        self._tw = TILE_W             # 当前格子宽（列表模式下＝画布宽）
        self._th = TILE_H
        self._sig = None
        self.empty_text = "这里是空的"
        self.rows = []                # [(name, size, isdir, rel, mtime)]
        self.sel = set()              # 选中的名字
        self._photos = {}             # name -> PhotoImage（必须持引用，否则被回收变空白）
        self._tiles = {}              # name -> (rect_id, img_id, txt_id, sub_id)
        self._cols = 0
        self._anchor = None
        self._requested = set()       # 已排队的缩略图，避免重复请求
        self._batch = 0               # 单次重绘已排队的数量
        self._rowmap = {}             # name -> (size, isdir, rel, mtime)

        self.canvas.bind("<Configure>", lambda e: self._on_resize())
        self.canvas.bind("<Button-1>", self._click)
        self.canvas.bind("<Control-Button-1>", self._ctrl_click)
        self.canvas.bind("<Shift-Button-1>", self._shift_click)
        self.canvas.bind("<Double-Button-1>", self._double)
        self.canvas.bind("<MouseWheel>", self._wheel)
        self.canvas.bind("<Motion>", self._motion)

    # ---- 数据
    def set_rows(self, rows):
        if rows == self.rows:
            return False
        old = {r[0] for r in self.rows}
        keep = {r[0] for r in rows}
        # 只保留仍然存在的文件的缩略图缓存，避免来回切目录时反复解码
        self._photos = {k: v for k, v in self._photos.items() if k in keep or k not in old}
        self.rows = rows
        self._rowmap = {r[0]: (r[1], r[2], r[3], r[4]) for r in rows}
        self.sel &= keep
        self._draw(force=True)
        return True

    def set_view(self, mode):
        """grid=大缩略图 / list=一行一个（文档多的时候靠名字扫更快）"""
        if mode == self.view:
            return
        self.view = mode
        self.canvas.configure(yscrollincrement=(ROW_H if mode == "list" else TILE_H // 3))
        self._draw(force=True)

    def rel_of(self, name):
        """这个名字相对当前目录的父路径（子目录搜索命中的才有值）。"""
        info = self._rowmap.get(name)
        return info[2] if info else ""

    def set_thumb(self, name, png_path):
        """缩略图生成好了：主线程加载并贴到格子里。

        占位块必须在这里被真正删除再换成图片——只改配置不动占位，
        画面上就仍是那个灰色方块，等于没出图。
        """
        if self.view != "grid":
            return                      # 列表视图没有图位，贴上会画到左上角
        d = self._tiles.get(name)
        if d is None or not self.canvas.winfo_exists():
            return
        try:
            photo = tk.PhotoImage(file=png_path)
        except Exception:
            return
        self._photos[name] = photo
        for pid in d.get("ph", ()):
            try:
                self.canvas.delete(pid)
            except tk.TclError:
                pass
        if d.get("img"):
            try:
                self.canvas.itemconfigure(d["img"], image=photo)
                return
            except tk.TclError:
                return
        try:
            d["img"] = self.canvas.create_image(d["cx"], d["cy"], image=photo)
        except tk.TclError:
            d["img"] = None

    def has_thumb(self, name):
        return name in self._photos

    def reset_thumbs(self):
        """换目录时调用：保留已生成的图，但允许同名文件重新排队。"""
        self._requested = set()
        self._batch = 0

    # ---- 布局
    def _on_resize(self):
        self._draw()

    def _cols_for(self):
        if self.view == "list":
            return 1
        w = max(self.canvas.winfo_width(), TILE_W)
        return max(1, int(w // TILE_W))

    def _sync_metrics(self):
        if self.view == "list":
            self._tw = max(self.canvas.winfo_width(), 260)
            self._th = ROW_H
        else:
            self._tw, self._th = TILE_W, TILE_H

    def _draw(self, force=False):
        cols = self._cols_for()
        self._sync_metrics()
        sig = (cols, self._tw, self.view)
        if not force and sig == self._sig and self._tiles:
            self._update_scroll()
            self._request_visible()
            return
        self._sig = sig
        self._cols = cols
        self.canvas.delete("all")
        self._tiles = {}
        self._batch = 0
        if not self.rows:
            self.canvas.create_text(
                10, 20, anchor="w", fill=MUTED, font=F_SMALL,
                text=self.empty_text)
            self.canvas.configure(scrollregion=(0, 0, 0, 0))
            return

        # 超大目录只画前 MAX_TILES 个：一次画几千个 canvas item 会明显卡顿
        shown = self.rows[:MAX_TILES]
        for i, row in enumerate(shown):
            r, c = divmod(i, cols)
            if self.view == "list":
                self._draw_row(r, row)
            else:
                self._draw_tile(r, c, row[0], row[1], row[2])

        if len(self.rows) > MAX_TILES:
            r, c = divmod(len(shown), cols)
            self.canvas.create_text(
                c * self._tw + 8, r * self._th + 20, anchor="w", fill=WARN,
                font=F_SMALL,
                text=f"…还有 {len(self.rows) - MAX_TILES} 项未显示（搜一下更快的）")
        self._update_scroll()
        self._request_visible()

    def _update_scroll(self):
        rows = (len(self.rows[:MAX_TILES]) + self._cols - 1) // self._cols
        h = max(rows * self._th + 12, self.canvas.winfo_height())
        self.canvas.configure(scrollregion=(0, 0, self._cols * self._tw, h))

    @staticmethod
    def _units(s):
        return sum(2 if ord(ch) > 0x2E80 else 1 for ch in s)

    @classmethod
    def _fit(cls, text, px, per=7):
        """按像素预算截断，超出用省略号。比 font.measure 快得多。"""
        budget = max(3, int(px / per))
        used, out = 0, ""
        for ch in text:
            u = 2 if ord(ch) > 0x2E80 else 1
            if used + u > budget - 1:
                return out + "…"
            out += ch
            used += u
        return out

    @classmethod
    def _fit_head(cls, text, px, per=7):
        """从头部省略：文件名的结尾（.docx）比开头的目录名重要。"""
        budget = max(3, int(px / per))
        used, out = 0, ""
        for ch in reversed(text):
            u = 2 if ord(ch) > 0x2E80 else 1
            if used + u > budget - 2:
                return "…" + out
            out = ch + out
            used += u
        return out

    def _draw_row(self, r, row):
        """列表视图：一行一个文件，名字在左，大小/时间在右。

        文档一多，缩略图网格反而更难扫——满屏都是同色的 DOC 方块。
        列表把名字排成整齐的一列，眼睛从上往下扫就快很多。
        """
        name, size, isdir, rel, mtime = row
        y0 = r * self._th
        w = self._tw
        cy = y0 + self._th // 2
        sel = name in self.sel
        kind, glyph, color = file_kind(name, isdir)
        rect = self.canvas.create_rectangle(
            2, y0 + 1, w - 4, y0 + self._th - 1,
            fill=("#e8efff" if sel else CARD),
            outline=("#eef1f6" if not sel else ACCENT), width=1)
        self.canvas.create_rectangle(6, y0 + 7, 9, y0 + self._th - 7,
                                     fill=color, outline="")

        dcol = 58 if mtime else 0
        scol = 56
        # 子目录命中的文件把所在文件夹拼在名字前面，从头部省略——
        # 这样一列文字就能说清「在哪 + 叫什么」，不用再挤第二列灰字
        label = name
        if rel:
            parts = rel.replace("\\", "/").split("/")
            short = parts[-1] if len(parts) == 1 else "…/" + parts[-1]
            label = short + "/" + name
        name_w = w - 22 - dcol - scol - 8
        shown = self._fit_head(label, name_w) if rel else self._fit(label, name_w)
        txt = self.canvas.create_text(15, cy, text=shown, anchor="w",
                                      fill=(ACCENT if sel else TEXT),
                                      font=("Microsoft YaHei UI", 9))
        if not isdir and size >= 0:
            self.canvas.create_text(w - 14 - dcol, cy, text=human_size(size),
                                    anchor="e", fill=MUTED,
                                    font=("Microsoft YaHei UI", 8))
        if mtime:
            self.canvas.create_text(w - 12, cy,
                                    text=time.strftime("%m-%d", time.localtime(mtime)),
                                    anchor="e", fill=MUTED,
                                    font=("Microsoft YaHei UI", 8))
        self._tiles[name] = {"rect": rect, "img": None, "txt": txt, "sub": None,
                             "ph": [], "cx": 0, "cy": cy}

    def _draw_tile(self, r, c, name, size, isdir):
        x0, y0 = c * TILE_W, r * TILE_H
        cx = x0 + TILE_W // 2
        sel = name in self.sel
        rect = self.canvas.create_rectangle(
            x0 + 4, y0 + 4, x0 + TILE_W - 4, y0 + TILE_H - 10,
            fill=("#e8efff" if sel else CARD),
            outline=(ACCENT if sel else CARD), width=2)
        kind, glyph, color = file_kind(name, isdir)

        img_id = None
        ph_ids = []
        photo = self._photos.get(name)
        if photo is not None:
            img_id = self.canvas.create_image(cx, y0 + 8 + THUMB_PX // 2,
                                              image=photo)
        else:
            # 还没缩略图：先画占位块，缩略图到了由 set_thumb 删掉换成真图
            box = 80 if kind == "dir" else 76
            top = y0 + 8 + (THUMB_PX - box) // 2
            ph_ids.append(self.canvas.create_rectangle(
                cx - box // 2, top, cx + box // 2, top + box,
                fill=color, outline=""))
            label = "文件夹" if isdir else (os.path.splitext(name)[1].lstrip(".").upper()[:5]
                                            or "文件")
            ph_ids.append(self.canvas.create_text(
                cx, top + box // 2, text=label, fill="#ffffff",
                font=("Microsoft YaHei UI", 11, "bold")))

        lines = self._wrap(name, 2)
        txt = self.canvas.create_text(cx, y0 + 8 + THUMB_PX + 6, text="\n".join(lines),
                                      fill=(ACCENT if sel else TEXT), font=("Microsoft YaHei UI", 8),
                                      anchor="n", justify="center", width=TILE_W - 8)
        sub = self.canvas.create_text(cx, y0 + TILE_H - 18,
                                      text=("" if (isdir or size < 0) else human_size(size)),
                                      fill=MUTED, font=("Microsoft YaHei UI", 7), anchor="n")
        # 先登记格子，再要缩略图：否则缓存命中时 set_thumb 会因「格子不存在」被丢掉
        self._tiles[name] = {"rect": rect, "img": img_id, "txt": txt, "sub": sub,
                             "ph": ph_ids, "cx": cx, "cy": y0 + 8 + THUMB_PX // 2}

    # ---- 只给看得见的格子取图
    def _request_visible(self):
        """大目录一次画几百格，如果全排队取缩略图，手机侧会被 adb 拖死。

        真实需求只有「我现在看得见的那几张」，所以按可视范围（上下各预取
        一屏）来要图，滚动一次补一批。
        """
        if not self.on_need_thumb or not self._tiles:
            return
        if self.view != "grid":
            # 列表视图不取图：几百个文档逐个 adb 抓一遍会把手机侧拖死，
            # 而列表的用途本来就是扫文件名。要看内容切回缩略图。
            return
        try:
            top = self.canvas.canvasy(0) - self._th
            bot = top + self.canvas.winfo_height() + self._th * 2
        except tk.TclError:
            return
        for name, d in self._tiles.items():
            if name in self._requested:
                continue
            if not (top <= d["cy"] <= bot):
                continue
            info = self._rowmap.get(name)
            if not info or info[1]:                       # 目录不要图
                continue
            if info[2]:                                   # 子目录命中的不取（路径对不上）
                continue
            if file_kind(name, False)[0] != "img":
                continue
            if self._photos.get(name) is not None:        # 已经有图了
                continue
            if self._batch >= 240:                        # 兜底上限
                return
            self._requested.add(name)
            self._batch += 1
            self.on_need_thumb(name, info[0])

    def _on_scroll(self, *args):
        self.canvas.yview(*args)
        self.after(90, self._request_visible)

    @staticmethod
    def _wrap(name, max_lines):
        """按「中文算 2 格、ASCII 算 1 格」粗排两行，比 measure 快得多。"""
        budget = 17
        lines, cur, used = [], "", 0
        for ch in name:
            w = 2 if ord(ch) > 0x2E80 else 1
            if used + w > budget and len(lines) < max_lines - 1:
                lines.append(cur)
                cur, used = "", 0
            cur += ch
            used += w
        if cur:
            lines.append(cur)
        if len(lines) > max_lines:
            lines = lines[:max_lines]
        if len(lines) == max_lines:
            last = lines[-1]
            if sum(1 for _ in last) >= budget - 1:
                lines[-1] = last[: max(1, len(last) - 1)] + "…"
        return lines or [""]

    # ---- 命中
    def _hit(self, x, y):
        gx, gy = self.canvas.canvasx(x), self.canvas.canvasy(y)
        if gx < 0 or gy < 0:
            return None
        c, r = int(gx // self._tw), int(gy // self._th)
        if c >= self._cols:
            return None
        idx = r * self._cols + c
        shown = self.rows[:MAX_TILES]
        if 0 <= idx < len(shown):
            return shown[idx][0]
        return None

    def _repaint_sel(self):
        off = "#eef1f6" if self.view == "list" else CARD
        for name, d in self._tiles.items():
            on = name in self.sel
            try:
                self.canvas.itemconfigure(d["rect"],
                                          fill=("#e8efff" if on else CARD),
                                          outline=(ACCENT if on else off))
                if d.get("txt"):
                    self.canvas.itemconfigure(d["txt"], fill=(ACCENT if on else TEXT))
            except tk.TclError:
                pass

    def _motion(self, event):
        name = self._hit(event.x, event.y)
        if not self.on_hint:
            return
        if not name:
            self.on_hint("")
            return
        row = next((r for r in self.rows if r[0] == name), None)
        if row:
            size, isdir, rel, mtime = row[1], row[2], row[3], row[4]
            where = f"  ·  在 {rel}" if rel else ""
            when = ("  ·  " + time.strftime("%Y-%m-%d %H:%M", time.localtime(mtime))
                    if mtime else "")
            self.on_hint(f"{name}{where}   "
                         + ("文件夹" if isdir else
                            (human_size(size) if size >= 0 else "大小未知")) + when)
        else:
            self.on_hint("")

    # ---- 交互
    def _click(self, event):
        self.canvas.focus_set()
        name = self._hit(event.x, event.y)
        self.sel = {name} if name else set()
        self._anchor = name
        self._repaint_sel()

    def _ctrl_click(self, event):
        name = self._hit(event.x, event.y)
        if not name:
            return
        if name in self.sel:
            self.sel.discard(name)
        else:
            self.sel.add(name)
        self._anchor = name
        self._repaint_sel()

    def _shift_click(self, event):
        name = self._hit(event.x, event.y)
        if not name:
            return
        names = [r[0] for r in self.rows[:MAX_TILES]]
        a = self._anchor if self._anchor in names else names[0]
        i, j = names.index(a), names.index(name)
        self.sel = set(names[min(i, j): max(i, j) + 1])
        self._repaint_sel()

    def _double(self, event):
        name = self._hit(event.x, event.y)
        if not name:
            return
        isdir = next((r[2] for r in self.rows if r[0] == name), False)
        self.on_open(name, isdir)

    def _wheel(self, event):
        delta = -1 if event.delta > 0 else 1
        self.canvas.yview_scroll(delta * 3, "units")
        self.after(90, self._request_visible)

    # ---- 供拖拽使用
    def selection(self):
        return [n for n in [r[0] for r in self.rows] if n in self.sel]

    def select_only(self, name):
        self.sel = {name}
        self._repaint_sel()

    def name_at_root(self, x_root, y_root):
        """给拖拽落点用：窗口坐标 → 格子里的名字。"""
        try:
            x = x_root - self.canvas.winfo_rootx()
            y = y_root - self.canvas.winfo_rooty()
        except tk.TclError:
            return None
        if x < 0 or y < 0 or x > self.canvas.winfo_width() or y > self.canvas.winfo_height():
            return None
        return self._hit(x, y)

    def contains_root(self, x_root, y_root):
        try:
            x, y = self.canvas.winfo_rootx(), self.canvas.winfo_rooty()
            return (x <= x_root <= x + self.canvas.winfo_width()
                    and y <= y_root <= y + self.canvas.winfo_height())
        except tk.TclError:
            return False


def card(master, **kw):
    f = tk.Frame(master, bg=CARD, highlightthickness=1, highlightbackground=BORDER, **kw)
    return f


def section_title(parent, text, desc=None):
    tk.Label(parent, text=text, bg=BG, fg=TEXT, font=F_TITLE, anchor="w").pack(fill="x")
    if desc:
        tk.Label(parent, text=desc, bg=BG, fg=MUTED, font=F_SUB,
                 anchor="w", justify="left").pack(fill="x", pady=(4, 0))


# ---------------------------------------------------------------- 主程序
class App:
    def __init__(self, root):
        self.root = root
        self.log_q = queue.Queue()
        self.ui_q = queue.Queue()
        self.cast_procs = []
        self.side_procs = []
        self.busy = False

        # 手机侧默认停在「下载与接收」——就是手机文件管理器里那一栏，
        # 拖进去的东西立刻能在手机上看得到（/sdcard/我的文件 是系统私有目录，不能用）
        self.current_path = PHONE_HOME
        # 电脑侧默认停在桌面：拖拽的出发地就是你自己看得见的那些文件
        _desk = os.path.join(os.path.expanduser("~"), "Desktop")
        self.local_path = _desk if os.path.isdir(_desk) else os.path.join(BASE, "接收文件")
        self.entries = []                                   # 手机侧当前显示的条目
        self.local_entries = []                             # 电脑侧当前显示的条目
        self._rows_cur = {"local": [], "phone": []}         # 当前目录全量扫描结果
        self._rows_deep = {"local": [], "phone": []}        # 子目录搜索命中
        self._find = {s: {"kw": "", "kind": "", "sort": "name", "deep": False}
                      for s in ("local", "phone")}
        self._find_widgets = {}
        self._find_after = {}
        self._search_gen = {"local": 0, "phone": 0}
        self._deep_busy = {"local": False, "phone": False}
        self._deep_sig = {"local": None, "phone": None}
        self._view_btns = {}
        self._pane_sig = {"local": None, "phone": None}     # 上次快照签名，没变化就不碰界面
        self._watch_on = False                              # 文件页是否可见（可见才轮询）
        self._last_change = time.perf_counter()             # 两栏最后一次出现差异的时刻
        self._phone_busy = False                            # 手机侧扫描是否在跑，防止 adb 堆叠
        self._drag = None                                   # 拖拽会话状态
        self._thumb_q = queue.Queue()                       # 缩略图生成队列（后台线程慢慢做）
        self._grid_dir = {"local": None, "phone": None}     # 每栏当前绘制的是哪个目录
        self.apps = []
        self.pages = {}
        self.navs = {}
        self.recording = False

        root.title("高效交互平台")
        root.geometry("1060x720")
        root.minsize(940, 620)
        root.configure(bg=BG)

        self._build_sidebar()
        self._build_main()
        self._init_desktop_drop()
        threading.Thread(target=self._thumb_loop, daemon=True).start()

        self.root.after(200, self._drain_log)
        self.show_page("cast")
        self.refresh_device()
        # 等设备检测跑完再加载目录，避免被 busy 挡掉
        self.root.after(2500, self._initial_browse)
        self.log("平台已就绪，工具目录：" + BASE)

    # ---------------- 左侧
    def _build_sidebar(self):
        side = tk.Frame(self.root, bg=SIDEBAR, width=196,
                        highlightthickness=1, highlightbackground=BORDER)
        side.pack(side="left", fill="y")
        side.pack_propagate(False)

        head = tk.Frame(side, bg=SIDEBAR)
        head.pack(fill="x", padx=18, pady=(20, 14))
        tk.Label(head, text="高效交互平台", bg=SIDEBAR, fg=TEXT,
                 font=("Microsoft YaHei UI", 13, "bold")).pack(anchor="w")
        tk.Label(head, text="手机与电脑协同工作台", bg=SIDEBAR, fg=MUTED,
                 font=F_SMALL).pack(anchor="w", pady=(2, 0))

        tk.Frame(side, bg=BORDER, height=1).pack(fill="x", padx=14)

        items = [
            ("cast",  "投屏",   "把手机屏幕投到电脑"),
            ("multi", "多开",   "同时运行多个应用"),
            ("file",  "文件",   "手机与电脑互传"),
        ]
        for key, text, hint in items:
            nav = NavItem(side, text, hint, lambda k=key: self.show_page(k))
            nav.pack(fill="x")
            self.navs[key] = nav

        # 底部设备状态
        bottom = tk.Frame(side, bg=SIDEBAR)
        bottom.pack(side="bottom", fill="x", padx=16, pady=16)
        tk.Frame(bottom, bg=BORDER, height=1).pack(fill="x", pady=(0, 12))
        self.dot = tk.Label(bottom, text="●", bg=SIDEBAR, fg=MUTED,
                            font=("Microsoft YaHei UI", 10))
        self.dot.pack(anchor="w")
        self.dev_name = tk.Label(bottom, text="检测设备中…", bg=SIDEBAR, fg=TEXT,
                                 font=("Microsoft YaHei UI", 10, "bold"), anchor="w")
        self.dev_name.pack(anchor="w")
        self.dev_info = tk.Label(bottom, text="", bg=SIDEBAR, fg=MUTED,
                                 font=F_SMALL, anchor="w")
        self.dev_info.pack(anchor="w", pady=(1, 8))
        FlatButton(bottom, "重新检测", self.refresh_device,
                   padx=10, pady=5).pack(anchor="w")
        # 先建好但不显示：只有诊断出是驱动层问题时才 pack 出来，
        # 否则正常用户会看到一个不知道什么时候该点的按钮
        self.usb_fix_btn = FlatButton(bottom, "修复 USB 接口", self.open_usb_fix,
                                      primary=True, padx=10, pady=5)

    # ---------------- 右侧
    def _build_main(self):
        right = tk.Frame(self.root, bg=BG)
        right.pack(side="left", fill="both", expand=True)

        # 日志先占住底部：否则上面的内容会把它压成一条缝
        logwrap = tk.Frame(right, bg=BG)
        logwrap.pack(side="bottom", fill="x", padx=22, pady=(10, 14))
        tk.Label(logwrap, text="运行日志", bg=BG, fg=MUTED, font=F_SMALL,
                 anchor="w").pack(fill="x", pady=(0, 4))
        self.logbox = tk.Text(logwrap, height=5, bg="#20242e", fg="#c9d1e0",
                              relief="flat", font=F_MONO, padx=12, pady=8,
                              insertbackground="#c9d1e0", wrap="none")
        self.logbox.pack(fill="x")
        self.logbox.configure(state="disabled")

        self.container = tk.Frame(right, bg=BG)
        self.container.pack(fill="both", expand=True, padx=22, pady=(18, 0))

        self.pages["cast"] = self._page_cast()
        self.pages["multi"] = self._page_multi()
        self.pages["file"] = self._page_file()

    def show_page(self, key):
        self._page_key = key
        for k, nav in self.navs.items():
            nav.set_active(k == key)
        for k, page in self.pages.items():
            page.pack_forget()
        self.pages[key].pack(fill="both", expand=True)
        # 只有文件页可见时才轮询：离开就停，避免后台空转、也避免抢占 adb
        if key == "file":
            self._start_watch()
        else:
            self._stop_watch()

    # ---------------- 页面：投屏
    def _page_cast(self):
        page = tk.Frame(self.container, bg=BG)
        section_title(page, "投屏", "把手机真实屏幕投到电脑上，用鼠标键盘直接操作手机。")

        body = tk.Frame(page, bg=BG)
        body.pack(fill="both", expand=True, pady=(16, 0))

        c1 = card(body)
        c1.pack(fill="x", padx=0, pady=0)
        inner = tk.Frame(c1, bg=CARD)
        inner.pack(fill="x", padx=20, pady=18)

        tk.Label(inner, text="画面参数", bg=CARD, fg=TEXT,
                 font=("Microsoft YaHei UI", 10, "bold")).grid(row=0, column=0,
                                                               columnspan=4, sticky="w")

        tk.Label(inner, text="分辨率", bg=CARD, fg=MUTED, font=F_BODY).grid(
            row=1, column=0, sticky="w", pady=(12, 0))
        self.var_size = tk.StringVar(value="1080")
        ttk.Combobox(inner, textvariable=self.var_size, width=8, state="readonly",
                     values=["720", "1080", "1920", "2688"]).grid(
            row=1, column=1, sticky="w", padx=(10, 30), pady=(12, 0))

        tk.Label(inner, text="帧率", bg=CARD, fg=MUTED, font=F_BODY).grid(
            row=1, column=2, sticky="w", pady=(12, 0))
        self.var_fps = tk.StringVar(value="60")
        ttk.Combobox(inner, textvariable=self.var_fps, width=6, state="readonly",
                     values=["30", "60"]).grid(row=1, column=3, sticky="w",
                                               padx=(10, 0), pady=(12, 0))

        self.var_off = tk.BooleanVar(value=True)
        tk.Checkbutton(inner, text="启动后让手机熄屏（省电，推荐）",
                       variable=self.var_off, bg=CARD, fg=TEXT, font=F_BODY,
                       activebackground=CARD, selectcolor="#ffffff",
                       anchor="w", cursor="hand2").grid(
            row=2, column=0, columnspan=4, sticky="w", pady=(14, 0))

        row = tk.Frame(inner, bg=CARD)
        row.grid(row=3, column=0, columnspan=4, sticky="w", pady=(18, 0))
        FlatButton(row, "启动投屏", self.start_cast, primary=True).pack(side="left")
        FlatButton(row, "停止投屏", self.stop_cast).pack(side="left", padx=10)

        c2 = card(body)
        c2.pack(fill="x", pady=(14, 0))
        inner2 = tk.Frame(c2, bg=CARD)
        inner2.pack(fill="x", padx=20, pady=16)
        tk.Label(inner2, text="快捷键（按住左 Alt 再按字母）", bg=CARD, fg=TEXT,
                 font=("Microsoft YaHei UI", 10, "bold")).pack(anchor="w")
        keys = [("Alt + P", "电源 锁屏/唤醒"), ("Alt + H", "回到主页"), ("Alt + B", "返回上一级"),
                ("Alt + S", "多任务列表"), ("Alt + N", "下拉通知栏"), ("Alt + O", "手机熄屏/亮屏"),
                ("Alt + F", "全屏切换"), ("Alt + V", "粘贴电脑剪贴板"),
                ("Alt + ↑", "音量加"), ("Alt + ↓", "音量减")]
        grid = tk.Frame(inner2, bg=CARD)
        grid.pack(fill="x", pady=(10, 0))
        for i, (k, v) in enumerate(keys):
            cell = tk.Frame(grid, bg=CARD)
            cell.grid(row=i // 2, column=i % 2, sticky="w", padx=(0, 40), pady=5)
            tk.Label(cell, text=k, bg="#eef3ff", fg=ACCENT,
                     font=("Consolas", 9, "bold"), padx=8, pady=3).pack(side="left")
            tk.Label(cell, text="  " + v, bg=CARD, fg=MUTED, font=F_BODY).pack(side="left")

        return page

    # ---------------- 页面：多开
    def _page_multi(self):
        page = tk.Frame(self.container, bg=BG)
        section_title(page, "多开分屏", "每个应用开一个独立虚拟屏，可同时运行、互不干扰。")

        bar = tk.Frame(page, bg=BG)
        bar.pack(fill="x", pady=(14, 8))
        FlatButton(bar, "加载应用列表", self.load_apps, primary=True,
                   padx=14, pady=7).pack(side="left")
        self.var_search = tk.StringVar()
        self.var_search.trace_add("write", lambda *_: self.render_apps())
        ent = tk.Entry(bar, textvariable=self.var_search, width=24, font=F_BODY,
                       relief="flat", bg="#ffffff", fg=TEXT,
                       highlightthickness=1, highlightbackground=BORDER)
        ent.pack(side="left", padx=(10, 0), ipady=5)
        tk.Label(bar, text="输入名称筛选", bg=BG, fg=MUTED, font=F_SMALL).pack(side="left", padx=8)
        FlatButton(bar, "关闭全部副屏", self.close_side, padx=12, pady=7).pack(side="right")

        wrap = tk.Frame(page, bg=CARD, highlightthickness=1, highlightbackground=BORDER)
        wrap.pack(fill="both", expand=True)
        self.tree_apps = ttk.Treeview(wrap, columns=("name", "pkg"), show="headings",
                                      selectmode="extended")
        self.tree_apps.heading("name", text="应用名称")
        self.tree_apps.heading("pkg", text="包名")
        self.tree_apps.column("name", width=260, anchor="w")
        self.tree_apps.column("pkg", width=420, anchor="w")
        sb = ttk.Scrollbar(wrap, orient="vertical", command=self.tree_apps.yview)
        self.tree_apps.configure(yscrollcommand=sb.set)
        self.tree_apps.pack(side="left", fill="both", expand=True, padx=(1, 0), pady=1)
        sb.pack(side="right", fill="y")
        self.tree_apps.bind("<Double-1>", lambda e: self.open_selected())

        foot = tk.Frame(page, bg=BG)
        foot.pack(fill="x", pady=(10, 0))
        FlatButton(foot, "打开选中的应用", self.open_selected, primary=True).pack(side="left")
        tk.Label(foot, text="按住 Ctrl 或 Shift 可多选；建议不超过 3 个，否则手机会发烫掉帧",
                 bg=BG, fg=MUTED, font=F_SMALL).pack(side="left", padx=12)

        return page

    # ---------------- 页面：文件（左电脑 / 右手机，拖拽互传，实时刷新）
    def _page_file(self):
        page = tk.Frame(self.container, bg=BG)
        section_title(page, "文件互传",
                      "左边电脑、右边手机，图片直接显示缩略图。文件多的时候用每栏的「找」框搜名字，"
                      "勾上「含子目录」连文件夹里的一起翻。拖进右栏就进手机，拖到某个文件夹上就放进那里。")

        split = tk.Frame(page, bg=BG)
        split.pack(fill="both", expand=True, pady=(14, 0))
        split.columnconfigure(0, weight=1, uniform="pane")
        split.columnconfigure(2, weight=1, uniform="pane")
        split.rowconfigure(0, weight=1)

        self._build_pane(split, 0, "电脑", "local")
        mid = tk.Frame(split, bg=BG, width=54)
        mid.grid(row=0, column=1, sticky="ns")
        mid.grid_propagate(False)
        tk.Label(mid, text="⇄", bg=BG, fg=MUTED, font=("Segoe UI", 18)).pack(expand=True)
        self._build_pane(split, 2, "手机", "phone")

        foot = tk.Frame(page, bg=BG)
        foot.pack(fill="x", pady=(10, 0))
        FlatButton(foot, "看手机屏幕", lambda: self.show_page("cast")).pack(side="left")
        FlatButton(foot, "截取手机画面", self.screenshot).pack(side="left", padx=8)
        self.rec_btn = FlatButton(foot, "录制视频", self.toggle_record)
        self.rec_btn.pack(side="left")
        FlatButton(foot, "打开接收文件夹", self.open_recv).pack(side="left", padx=8)
        FlatButton(foot, "刷新两边", self.refresh_both, padx=12).pack(side="left")
        tk.Label(foot, text="拖进来即入手机 · Ctrl+F 找文件", bg=BG, fg=MUTED,
                 font=F_SMALL).pack(side="left", padx=10)

        def _focus_find(event=None):
            if getattr(self, "_page_key", "") == "file":
                w = self._find_widgets.get("local")
                if w:
                    w["entry"].focus_set()
                    w["entry"].select_range(0, "end")
                return "break"

        self.root.bind("<Control-f>", _focus_find)
        self.root.bind("<Control-F>", _focus_find)

        return page

    def _build_pane(self, parent, col, title, side):
        """构建一栏文件列表。side: local=电脑 / phone=手机"""
        pad = (0, 8) if side == "local" else (8, 0)
        pane = tk.Frame(parent, bg=BG)
        pane.grid(row=0, column=col, sticky="nsew", padx=pad)
        pane.columnconfigure(0, weight=1)
        pane.rowconfigure(4, weight=1)

        head = tk.Frame(pane, bg=BG)
        head.grid(row=0, column=0, sticky="ew")
        tk.Label(head, text=title, bg=BG, fg=TEXT,
                 font=("Microsoft YaHei UI", 10, "bold")).pack(side="left")

        bar = tk.Frame(pane, bg=BG)
        bar.grid(row=1, column=0, sticky="ew", pady=(6, 4))
        FlatButton(bar, "↑ 上级", lambda: self.pane_up(side), padx=10, pady=4).pack(side="left")
        FlatButton(bar, "刷新", lambda: self.pane_refresh(side), padx=10, pady=4).pack(
            side="left", padx=6)
        vbtn = FlatButton(bar, "列", lambda: self.toggle_view(side), padx=8, pady=4, width=2)
        vbtn.pack(side="right")
        self._view_btns[side] = vbtn
        if side == "local":
            FlatButton(bar, "…", self.pick_local_dir, padx=10, pady=4).pack(side="left")
        label = tk.Label(bar, text="", bg="#eef1f6", fg=TEXT, font=("Consolas", 8),
                         padx=6, pady=3, anchor="w")
        label.pack(side="left", fill="x", expand=True, padx=(6, 6))

        self._build_find_bar(pane, side)

        wrap = tk.Frame(pane, bg=CARD, highlightthickness=1, highlightbackground=BORDER)
        wrap.grid(row=4, column=0, sticky="nsew")
        wrap.columnconfigure(0, weight=1)
        wrap.rowconfigure(0, weight=1)

        grid = FileGrid(wrap, side,
                        on_open=lambda n, d, s=side: self.pane_open(n, d, s),
                        on_need_thumb=lambda n, sz, s=side: self.need_thumb(s, n, sz),
                        on_hint=self._tile_hint)
        grid.pack(fill="both", expand=True, padx=1, pady=1)

        hint = tk.Label(pane, text="", bg=BG, fg=MUTED, font=F_SMALL, anchor="w")
        hint.grid(row=5, column=0, sticky="ew", pady=(4, 0))

        # 拖拽：按下/移动/松开由 App 接管，FileGrid 自己的点击选中仍然保留
        grid.canvas.bind("<ButtonPress-1>", lambda e, s=side: self._drag_start(e, s), add="+")
        grid.canvas.bind("<B1-Motion>", self._drag_move, add="+")
        grid.canvas.bind("<ButtonRelease-1>", lambda e, s=side: self._drag_drop(e, s), add="+")

        if side == "local":
            self.grid_local, self.local_label, self.local_hint = grid, label, hint
        else:
            self.grid_phone, self.phone_label, self.phone_hint = grid, label, hint
            self.path_label = label      # 保留旧属性名，避免其它位置引用失效
            self._build_places(pane)

    def _build_find_bar(self, pane, side):
        """每栏一个「找文件」条。

        文档一多，光靠眼睛在网格里扫是不现实的（一堆 WPS/PDF 长得一样），
        所以给一条：关键词 + 类型 + 排序 + 是否连子目录一起搜。
        关键词空格分词＝同时满足，中文直接输，不区分大小写。

        类型和排序用「点一下就换下一个」的小标签，不用下拉框：选项只有
        五六个，少一次点击，也不会把这一行挤爆。
        """
        bar = tk.Frame(pane, bg=BG)
        bar.grid(row=2, column=0, sticky="ew", pady=(0, 5))
        tk.Label(bar, text="找", bg=BG, fg=MUTED, font=F_SMALL).pack(side="left")

        ent = tk.Entry(bar, bg="#ffffff", fg=TEXT, relief="flat",
                       highlightthickness=1, highlightbackground=BORDER,
                       highlightcolor=ACCENT, font=("Microsoft YaHei UI", 9),
                       insertbackground=TEXT)
        ent.pack(side="left", fill="x", expand=True, padx=(4, 5), ipady=3)
        ent.bind("<KeyRelease>", lambda e, s=side: self._find_changed(s))
        ent.bind("<Return>", lambda e, s=side: self._find_changed(s, now=True))
        ent.bind("<Escape>", lambda e, s=side: self._clear_find(s))

        kbtn = self._chip(bar, KIND_CHOICES[0][0], lambda s=side: self._cycle(s, "kind"))
        sbtn = self._chip(bar, SORT_CHOICES[0][0], lambda s=side: self._cycle(s, "sort"))

        var = tk.BooleanVar(value=False)
        chk = tk.Checkbutton(bar, text="含子目录", variable=var, bg=BG, fg=TEXT,
                             activebackground=BG, activeforeground=TEXT,
                             selectcolor="#ffffff", font=F_SMALL, bd=0,
                             highlightthickness=0,
                             command=lambda s=side: self._find_changed(s, now=True))
        chk.pack(side="left")

        self._find_widgets[side] = {"entry": ent, "kind": kbtn, "sort": sbtn,
                                    "deep": chk, "var": var}

    @staticmethod
    def _chip(bar, text, cmd):
        """搜索条上的小按钮：点一下换下一个选项。"""
        lab = tk.Label(bar, text=text, bg="#ffffff", fg=TEXT,
                       font=("Microsoft YaHei UI", 8), padx=7, pady=4,
                       cursor="hand2", highlightthickness=1,
                       highlightbackground=BORDER)
        lab.pack(side="left", padx=(0, 3))
        lab.bind("<Button-1>", lambda e: cmd())
        lab.bind("<Enter>", lambda e: lab.configure(bg="#f0f4ff"))
        lab.bind("<Leave>", lambda e: lab.configure(bg="#ffffff"))
        return lab

    def _cycle(self, side, which):
        st = self._find[side]
        items = KIND_CHOICES if which == "kind" else SORT_CHOICES
        idx = next((i for i, (_, v) in enumerate(items) if v == st[which]), 0)
        txt, val = items[(idx + 1) % len(items)]
        st[which] = val
        self._find_widgets[side][which].config(text=txt)
        self._refresh_view(side)

    def toggle_view(self, side):
        grid = self.grid_local if side == "local" else self.grid_phone
        mode = "list" if grid.view == "grid" else "grid"
        grid.set_view(mode)
        self._view_btns[side].config(text=("图" if mode == "list" else "列"))
        self.log(("电脑" if side == "local" else "手机")
                 + ("侧：列表视图（一行一个，适合文档多时扫名字）"
                    if mode == "list" else "侧：缩略图视图"))

    def _build_places(self, pane):
        """手机侧「位置」栏：和手机文件管理器里的入口同名，点一下右侧就跳过去。

        这里用自制小标签而不是 FlatButton：需要「当前所在」高亮，而 FlatButton
        的悬停会直接改 bg，两者会打架。
        """
        bar = tk.Frame(pane, bg=BG)
        bar.grid(row=3, column=0, sticky="ew", pady=(0, 6))
        self.place_btns = {}
        for c in range(4):
            bar.columnconfigure(c, weight=1, uniform="place")
        for i, (text, path) in enumerate(PHONE_PLACES):
            lab = tk.Label(bar, text=text, bg="#ffffff", fg=TEXT,
                           font=("Microsoft YaHei UI", 8), padx=4, pady=3,
                           cursor="hand2", highlightthickness=1,
                           highlightbackground=BORDER)
            # 一行放不下 7 个入口，按 4 列折行：窗口窄也不会被边界吃掉
            lab.grid(row=i // 4, column=i % 4, sticky="ew", padx=(0, 4), pady=(0, 4))
            lab.bind("<Button-1>", lambda e, p=path: self.goto_place(p))
            lab.bind("<Enter>", lambda e, w=lab: w.configure(bg="#f0f4ff"))
            lab.bind("<Leave>", lambda e, w=lab: self._paint_place(w))
            self.place_btns[path] = lab
        self._mark_place()

    def _paint_place(self, lab):
        path = next((p for p, w in self.place_btns.items() if w is lab), None)
        on = path is not None and self.current_path.rstrip("/") == path.rstrip("/")
        lab.configure(bg=("#eaf1ff" if on else "#ffffff"),
                      fg=(ACCENT if on else TEXT),
                      highlightbackground=(ACCENT if on else BORDER))

    def _mark_place(self):
        for lab in getattr(self, "place_btns", {}).values():
            self._paint_place(lab)

    # ---------------- 从 Windows 桌面直接把文件拖进来
    def _init_desktop_drop(self):
        """让整个窗口接住从桌面/资源管理器拖来的文件（Win32 WM_DROPFILES）。

        tkinter 没有原生跨程序拖放，而这个 Python 环境没有 pip、装不了
        tkinterdnd2，所以直接用 DragAcceptFiles + 子类化窗口过程。
        任何一步失败都只记一条日志，不影响其它功能。
        """
        if os.name != "nt":
            return
        try:
            import ctypes
            from ctypes import wintypes

            user32 = ctypes.WinDLL("user32", use_last_error=True)
            shell32 = ctypes.WinDLL("shell32", use_last_error=True)

            hwnd = int(self.root.wm_frame(), 16)
            if not hwnd:
                return

            user32.GetWindowLongPtrW.restype = ctypes.c_void_p
            user32.GetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int]
            user32.SetWindowLongPtrW.restype = ctypes.c_void_p
            user32.SetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_void_p]
            user32.CallWindowProcW.restype = ctypes.c_ssize_t
            user32.CallWindowProcW.argtypes = [ctypes.c_void_p, wintypes.HWND, ctypes.c_uint,
                                               ctypes.c_size_t, ctypes.c_ssize_t]
            shell32.DragQueryFileW.restype = ctypes.c_uint
            shell32.DragQueryFileW.argtypes = [wintypes.HANDLE, ctypes.c_uint,
                                               wintypes.LPWSTR, ctypes.c_uint]

            old = user32.GetWindowLongPtrW(wintypes.HWND(hwnd), -4)   # GWLP_WNDPROC
            if not old:
                return

            WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, wintypes.HWND, ctypes.c_uint,
                                         ctypes.c_size_t, ctypes.c_ssize_t)
            self._drop_shell = shell32

            def _proc(h, msg, wp, lp):
                if msg == 0x0233:                                     # WM_DROPFILES
                    try:
                        self._win_drop(wp)
                    except Exception as exc:
                        self.log(f"拖放处理异常：{exc}", "err")
                    finally:
                        try:
                            shell32.DragFinish(wintypes.HANDLE(wp))
                        except Exception:
                            pass
                    return 0
                return user32.CallWindowProcW(old, h, msg, wp, lp)

            self._drop_proc = WNDPROC(_proc)          # 必须留引用，否则回调被回收会崩
            user32.SetWindowLongPtrW(wintypes.HWND(hwnd), -4,
                                     ctypes.cast(self._drop_proc, ctypes.c_void_p))
            shell32.DragAcceptFiles(wintypes.HWND(hwnd), True)
            self._drop_ready = True
        except Exception as exc:
            self._drop_ready = False
            self.log(f"桌面拖放未启用（不影响其它功能）：{exc}", "warn")

    def _win_drop(self, hdrop):
        import ctypes
        from ctypes import wintypes
        sh = self._drop_shell
        handle = wintypes.HANDLE(hdrop)
        n = sh.DragQueryFileW(handle, 0xFFFFFFFF, None, 0)
        paths = []
        for i in range(n):
            ln = sh.DragQueryFileW(handle, i, None, 0)
            buf = ctypes.create_unicode_buffer(ln + 1)
            sh.DragQueryFileW(handle, i, buf, ln + 1)
            if buf.value:
                paths.append(buf.value)
        if not paths:
            return
        target = self._drop_target_dir()
        self.show_page("file")
        self.log(f"从桌面拖入 {len(paths)} 项 → {target}", "ok")
        self.bg(self._push_to_phone, paths, target)

    def _drop_target_dir(self):
        """落在哪就放进哪：指针下面是手机栏里的文件夹，就进那个文件夹。"""
        try:
            x, y = self.root.winfo_pointerxy()
        except Exception:
            return self.current_path
        g = self.grid_phone
        if not g.contains_root(x, y):
            return self.current_path
        name = g.name_at_root(x, y)
        row = next((r for r in self.entries if r[0] == name), None)
        if row and row[2]:
            return self.current_path + name + "/"
        return self.current_path


    # ---------------------------------------------------------------- 日志
    def log(self, msg, level="info"):
        self.log_q.put((level, msg))

    def ui(self, fn, *args):
        """把界面更新排进主线程队列（Tk 不允许被子线程直接改）"""
        self.ui_q.put((fn, args))

    def _drain_log(self):
        # 1) 界面更新：只在主线程执行
        try:
            while True:
                fn, args = self.ui_q.get_nowait()
                try:
                    fn(*args)
                except Exception as e:
                    self.log_q.put(("err", f"界面更新失败：{e}"))
        except queue.Empty:
            pass

        # 2) 日志
        try:
            while True:
                level, msg = self.log_q.get_nowait()
                color = {"ok": "#5dd39e", "warn": "#f0b354", "err": "#ff7a7a"}.get(level, "#c9d1e0")
                ts = time.strftime("%H:%M:%S")
                self.logbox.configure(state="normal")
                self.logbox.insert("end", f"[{ts}] {msg}\n")
                self.logbox.tag_configure(level, foreground=color)
                self.logbox.tag_add(level, f"end-{len(msg)+12}c", "end-1c")
                self.logbox.see("end")
                self.logbox.configure(state="disabled")
        except queue.Empty:
            pass
        self.root.after(150, self._drain_log)

    def bg(self, fn, *args):
        if self.busy:
            self.log("上一个操作还没结束，请稍等…", "warn")
            return
        self.busy = True

        def wrapper():
            try:
                fn(*args)
            except Exception as e:
                self.log(f"出错：{e}", "err")
            finally:
                self.busy = False

        threading.Thread(target=wrapper, daemon=True).start()

    # ---------------------------------------------------------------- 设备
    def refresh_device(self):
        self.bg(self._refresh_device)

    def _apply_device(self, name, info, color):
        self.dev_name.config(text=name)
        self.dev_info.config(text=info)
        self.dot.config(fg=color)

    def _refresh_device(self):
        if not os.path.exists(ADB):
            self.ui(self._apply_device, "缺少 adb.exe", "请检查 scrcpy 目录", ERR)
            return

        states = adb_device_states()
        ready = [s for s in states if s[1] == "device"]
        if ready:
            serial = ready[0][0]
            rc, model = adb(["-s", serial, "shell", "getprop", "ro.product.model"])
            rc2, bat = adb(["-s", serial, "shell", "dumpsys", "battery"])
            m = re.search(r"level:\s*(\d+)", bat)
            level = m.group(1) if m else "?"
            charging = "AC powered: true" in bat or "USB powered: true" in bat
            self.ui(self._apply_device, model.strip() or "已连接",
                    f"电量 {level}%" + ("  ·  充电中" if charging else ""), OK)
            self.ui(self._hide_usb_fix)
            self.log(f"设备已连接：{model.strip()}（{level}%）", "ok")
            return

        # 插上了但没授权：这不是「没插」，是两个完全不同的故障，提示必须分开
        pending = [s for s in states if s[1] == "unauthorized"]
        if pending:
            self.ui(self._apply_device, "等待手机授权", "请在手机上点「允许 USB 调试」", WARN)
            self.ui(self._hide_usb_fix)
            self.log("设备已连接但未授权：请在手机屏幕上点「允许 USB 调试」，并勾选始终允许", "warn")
            return

        # 走到这里说明 adb 什么都看不到，下沉到 USB 层查具体卡在哪一步
        code, hint, detail = usb_diagnose()
        color = ERR if code in ("no-interface", "driver-error", "not-registered") else WARN
        self.ui(self._apply_device, "手机未连接", hint, color)
        self.log(detail, "warn")
        if code in ("driver-error", "not-registered"):
            self.ui(lambda: self.usb_fix_btn.pack(anchor="w", pady=(6, 0)))
        else:
            self.ui(self._hide_usb_fix)

    def _hide_usb_fix(self):
        # 用 winfo_manager 而不是 winfo_ismapped：窗口最小化或尚未映射时
        # ismapped 恒为假，会导致按钮明明还挂着却撤不掉。
        btn = getattr(self, "usb_fix_btn", None)
        if btn is not None and btn.winfo_manager():
            btn.pack_forget()

    def open_usb_fix(self):
        """启动 USB 修复工具。脚本自己会请求管理员权限。"""
        if not os.path.exists(USB_FIX_BAT):
            self.log(f"找不到修复工具：{USB_FIX_BAT}", "err")
            return
        try:
            os.startfile(USB_FIX_BAT)
            self.log("已启动 USB 修复工具（会弹出管理员权限确认），跑完点「重新检测」", "warn")
        except Exception as exc:
            self.log(f"启动修复工具失败：{exc}", "err")

    # ---------------------------------------------------------------- 投屏
    def start_cast(self):
        self.bg(self._start_cast)

    def _start_cast(self):
        serial = get_serial()
        if not serial:
            self.log("手机未连接", "err")
            return
        args = [SCRCPY, "-s", serial,
                f"--max-size={self.var_size.get()}",
                f"--max-fps={self.var_fps.get()}",
                "--no-audio", "--no-mouse-hover",
                # 把文件从电脑桌面拖进投屏窗口，就直接落到手机这个目录
                "--push-target=/sdcard/Download/",
                "--window-title=高效交互平台"]
        if self.var_off.get():
            args += ["--turn-screen-off", "--stay-awake"]
        try:
            p = subprocess.Popen(args, cwd=SCRCPY_DIR, **QUIET)
            self.cast_procs.append(p)
            self.log("投屏已启动", "ok")
        except Exception as e:
            self.log(f"启动失败：{e}", "err")

    def stop_cast(self):
        n = 0
        for p in list(self.cast_procs):
            try:
                p.terminate()
                n += 1
            except Exception:
                pass
        self.cast_procs.clear()
        self.log(f"已停止 {n} 个投屏窗口")

    # ---------------------------------------------------------------- 多开
    def load_apps(self):
        self.bg(self._load_apps)

    def _load_apps(self):
        self.log("读取应用列表（首次约 10 秒）…")
        rc, out = run([SCRCPY, "--list-apps"], timeout=180, cwd=SCRCPY_DIR)
        apps = []
        for line in out.splitlines():
            m = re.match(r"^\s*[-*+]\s+(.+?)\s{2,}([A-Za-z0-9_.]+)\s*$", line)
            if m:
                apps.append((m.group(1).strip(), m.group(2)))
        apps.sort(key=lambda x: x[0])
        self.apps = apps
        self.ui(self.render_apps)
        self.log(f"已加载 {len(apps)} 个应用", "ok")

    def render_apps(self):
        kw = self.var_search.get().strip().lower()
        self.tree_apps.delete(*self.tree_apps.get_children())
        for name, pkg in self.apps:
            if kw and kw not in name.lower() and kw not in pkg.lower():
                continue
            self.tree_apps.insert("", "end", values=(name, pkg))

    def open_selected(self):
        items = self.tree_apps.selection()
        if not items:
            self.log("先选中要打开的应用", "warn")
            return
        rows = [self.tree_apps.item(i, "values") for i in items]
        self.bg(self._open_apps, rows)

    def _open_apps(self, rows):
        serial = get_serial()
        if not serial:
            self.log("手机未连接", "err")
            return
        n = 0
        for name, pkg in rows:
            args = [SCRCPY, "-s", serial, "--new-display=720x1280/320",
                    f"--start-app={pkg}", "--no-audio", "--max-fps=30",
                    "--max-size=720", "--no-mouse-hover",
                    f"--window-title={name}"]
            try:
                p = subprocess.Popen(args, cwd=SCRCPY_DIR, **QUIET)
                self.side_procs.append(p)
                n += 1
                self.log(f"已开启副屏：{name}", "ok")
                time.sleep(0.8)
            except Exception as e:
                self.log(f"{name} 启动失败：{e}", "err")
        self.log(f"共开启 {n} 个副屏")

    def close_side(self):
        n = 0
        for p in list(self.side_procs):
            try:
                p.terminate()
                n += 1
            except Exception:
                pass
        self.side_procs.clear()
        self.log(f"已关闭 {n} 个副屏")

    # ---------------------------------------------------------------- 文件页
    # 两边都靠轮询：手机侧拿不到文件变更事件（inotify 多数机型不可用），
    # 统一成轮询才不会一边实时、一边卡在旧数据上。轮询只在文件页可见时跑。
    def _start_watch(self):
        if self._watch_on:
            return
        self._watch_on = True
        self._last_change = time.perf_counter()
        self._pane_sig = {"local": None, "phone": None}
        self._scan_local()
        self._scan_phone_async()
        self._tick_watch()

    def _stop_watch(self):
        self._watch_on = False

    def _tick_watch(self):
        if not self._watch_on:
            return
        self._scan_local()
        # 传输进行中时暂停手机侧轮询：否则每 1.5 秒一条 adb shell ls
        # 会和 adb pull/push 抢同一个 transport，拖慢传输甚至让 ls 超时
        if not self.busy:
            self._scan_phone_async()
        # 自适应降频：连续静止就把间隔拉长到 6 秒，一有变化立刻回到 1.5 秒。
        # 手机侧每次扫描要新建 adb 进程（实测约 260ms），静止时按 1.5 秒刷
        # 等于长期占用 1/6 的 CPU，整个程序都会跟着发钝。
        quiet = time.perf_counter() - self._last_change
        steps = int(quiet * 1000 // WATCH_MS)
        if steps >= 6:                                  # 约 9 秒无变化
            delay = WATCH_MS_MAX
        elif steps >= 3:                                # 约 4.5 秒无变化
            delay = WATCH_MS * 2
        else:
            delay = WATCH_MS
        self.root.after(delay, self._tick_watch)

    def _initial_browse(self):
        """启动后补一次目录加载。

        只在用户还没自己翻过目录时才生效——否则启动 2.5 秒后正看着某个
        文件夹，界面会突然被拽回「下载与接收」。
        """
        if self.current_path == PHONE_HOME:
            self.browse(PHONE_HOME)

    def refresh_both(self):
        self._pane_sig = {"local": None, "phone": None}
        self._scan_local()
        self._scan_phone_async()
        self.log("已刷新两边目录")

    def pane_refresh(self, side):
        self._pane_sig[side] = None
        if side == "local":
            self._scan_local()
        else:
            self._scan_phone_async()

    # ---- 电脑侧：scandir 很快，主线程直接做
    def _scan_local(self):
        try:
            rows = []
            with os.scandir(self.local_path) as it:
                for ent in it:
                    try:
                        isdir = ent.is_dir()
                        stt = ent.stat()
                        size = 0 if isdir else stt.st_size
                        mtime = stt.st_mtime
                    except OSError:
                        isdir, size, mtime = False, 0, 0
                    rows.append((ent.name, size, isdir, "", mtime))
        except OSError as exc:
            self.local_hint.config(text="电脑侧读不到这个目录")
            self.log(f"电脑侧读取失败：{exc}", "err")
            return
        rows.sort(key=lambda x: (not x[2], x[0].lower()))
        self._apply_rows("local", rows)

    # ---- 手机侧：adb 调用必须离开主线程，否则界面会卡住
    def _scan_phone_async(self):
        if self._phone_busy:
            return
        self._phone_busy = True
        threading.Thread(target=self._scan_phone, daemon=True).start()

    def _scan_phone(self):
        path = self.current_path
        try:
            serial = get_serial()
            if not serial:
                self.ui(self._phone_offline)
                return
            cmd = f"ls -l '{path}' 2>/dev/null"
            out = adb_shell(serial, cmd, timeout=15)
            if out is None:                      # 常驻会话不可用 -> 退回一次性调用
                rc, out = adb(["-s", serial, "shell", cmd], timeout=20)
                out = out or ""
            rows = []
            for line in out.splitlines():
                m = re.match(
                    r"^([dl-])[rwxsStT-]{9}\s+\d+\s+\S+\s+\S+\s+(\d+)\s+[\d-]+\s+[\d:]+\s+(.+)$",
                    line.strip())
                if not m:
                    continue
                name = m.group(3).strip()
                if name in (".", ".."):
                    continue
                mt = 0.0
                m2 = re.search(r"(\d{4})-(\d{2})-(\d{2})\s+(\d{2}):(\d{2})", line)
                if m2:
                    try:
                        mt = time.mktime((int(m2.group(1)), int(m2.group(2)),
                                          int(m2.group(3)), int(m2.group(4)),
                                          int(m2.group(5)), 0, 0, 0, -1))
                    except (ValueError, OverflowError):
                        mt = 0.0
                rows.append((name, int(m.group(2)), m.group(1) == "d", "", mt))
            rows.sort(key=lambda x: (not x[2], x[0].lower()))
            self.ui(self._apply_rows, "phone", rows)
        except Exception as exc:
            self.ui(self._phone_hint_err, str(exc))
        finally:
            self._phone_busy = False
            # 扫描期间用户又切了目录：立即补一次，否则界面会停在上一个目录，
            # 要等下一轮轮询（静止时可能 6 秒后）才纠正过来
            if self.current_path != path:
                self._scan_phone_async()

    def _phone_offline(self):
        self.phone_hint.config(text="手机未连接（插线，或先 adb connect）")

    def _phone_hint_err(self, msg):
        self.phone_hint.config(text="手机读取失败：" + msg[:40])

    # ---- 主线程：数据真变了才重画，画面没变化就一个像素都不动
    def _apply_rows(self, side, rows):
        if self._pane_sig[side] == rows:
            return
        self._pane_sig[side] = rows
        self._last_change = time.perf_counter()      # 有变化 -> 轮询回到高频
        self._rows_cur[side] = rows
        # 目录内容刷新了也要按当前搜索条件重新筛，不能把筛选结果冲掉
        self._paint_rows(side)

    def _base_of(self, side):
        return self.local_path if side == "local" else self.current_path

    def _dir_changed(self, side):
        """换了目录：作废签名、子目录搜索结果和缩略图排队记录。"""
        self._pane_sig[side] = None
        self._rows_deep[side] = []
        self._deep_sig[side] = None
        self._deep_busy[side] = False
        self._search_gen[side] += 1
        # 带着搜索词换目录时，得在新目录里重新翻一遍子目录，
        # 否则界面会显示「找到 N 项」却少了子目录里的那些
        st = self._find[side]
        if st["kw"] and st["deep"]:
            self.root.after(0, lambda s=side: self._refresh_view(s))

    def _paint_rows(self, side):
        """按「关键词 + 类型 + 排序」算出该显示哪些，再交给网格。"""
        st = self._find[side]
        cur = self._rows_cur[side]
        deep = self._rows_deep[side]
        use_deep = bool(st["kw"]) and st["deep"]

        if use_deep:
            # 深度搜索时保留当前目录的子文件夹，否则搜完就没法往回进了
            pool = [r for r in cur if r[2]] + deep
        else:
            pool = list(cur)

        kws = [w for w in st["kw"].lower().split() if w]
        kind = st["kind"]
        out = []
        for r in pool:
            name, size, isdir, rel, mtime = r
            if kind and (isdir or file_kind(name, isdir)[0] != kind):
                continue
            if kws:
                hay = (name + " " + rel).lower()
                if not all(w in hay for w in kws):
                    continue
            out.append(r)

        if st["sort"] == "new":
            out.sort(key=lambda x: (-(x[4] or 0), x[0].lower()))
        elif st["sort"] == "size":
            out.sort(key=lambda x: (x[2], -(x[1] if x[1] >= 0 else 0), x[0].lower()))
        else:
            out.sort(key=lambda x: (not x[2], x[0].lower()))

        grid = self.grid_local if side == "local" else self.grid_phone
        label = self.local_label if side == "local" else self.phone_label
        hint = self.local_hint if side == "local" else self.phone_hint

        cur_dir = self._base_of(side)
        if self._grid_dir[side] != cur_dir:
            # 换了目录：清掉「已排队」记录，新目录里的同名文件才会重新取缩略图
            self._grid_dir[side] = cur_dir
            grid.reset_thumbs()

        filtering = bool(st["kw"] or kind)
        grid.empty_text = ("没有匹配的文件（按 Esc 清空搜索）" if filtering else "这里是空的")
        grid.set_rows(out)

        if side == "local":
            self.local_entries = out
            label.config(text=self.local_path)
        else:
            self.entries = out
            label.config(text=self.current_path)
            self._mark_place()

        if filtering:
            msg = f"找到 {len(out)} 项"
            if use_deep and self._deep_busy[side]:
                msg += "（正在翻子目录…）"
            elif use_deep:
                msg += "（含子目录）"
            hint.config(text=msg)
        else:
            hint.config(text=f"{len(cur)} 项")

    # ---- 搜索：输入防抖 + 子目录深度搜索都放后台
    def _find_changed(self, side, now=False):
        w = self._find_widgets.get(side)
        if not w:
            return
        st = self._find[side]
        kw = w["entry"].get().strip()
        kind = st["kind"]                 # 类型/排序由 _cycle 直接改状态
        sort = st["sort"]
        deep = bool(w["var"].get())
        if (kw, kind, sort, deep) == (st["kw"], st["kind"], st["sort"], st["deep"]):
            return
        st.update(kw=kw, kind=kind, sort=sort, deep=deep)

        old = self._find_after.get(side)
        if old:
            try:
                self.root.after_cancel(old)
            except Exception:
                pass
            self._find_after[side] = None
        if now:
            self._refresh_view(side)
        else:
            # 每敲一个字就重算的话，手机侧会被 adb 请求打爆；等手停下来
            self._find_after[side] = self.root.after(
                260, lambda s=side: self._refresh_view(s))

    def _clear_find(self, side):
        w = self._find_widgets.get(side)
        if not w:
            return
        if w["entry"].get():
            w["entry"].delete(0, "end")
        w["kind"].config(text=KIND_CHOICES[0][0])
        w["var"].set(False)
        self._find[side].update(kw="", kind="", deep=False)
        self._refresh_view(side)

    def _refresh_view(self, side):
        self._find_after[side] = None
        st = self._find[side]
        base = self._base_of(side)
        if st["kw"] and st["deep"]:
            sig = (base, tuple(st["kw"].lower().split()), st["kind"])
            if self._deep_sig[side] != sig:
                self._deep_sig[side] = sig
                self._search_gen[side] += 1
                gen = self._search_gen[side]
                self._rows_deep[side] = []
                self._deep_busy[side] = True
                if side == "local":
                    self._deep_local(base, gen)
                else:
                    self._deep_phone(base, gen)
        else:
            self._deep_sig[side] = None
            self._rows_deep[side] = []
            self._deep_busy[side] = False
        self._paint_rows(side)

    def _deep_local(self, base, gen):
        kws = [w for w in self._find["local"]["kw"].lower().split() if w]

        def work():
            out = []
            try:
                for dirpath, dirnames, filenames in os.walk(base):
                    if gen != self._search_gen["local"]:
                        return
                    dirnames[:] = [d for d in dirnames if not d.startswith(".")]
                    rel = os.path.relpath(dirpath, base)
                    rel = "" if rel == "." else rel
                    if rel and rel.count(os.sep) + 1 > 4:
                        dirnames[:] = []
                        continue
                    low_rel = rel.lower()
                    for fn in filenames:
                        if fn.startswith("."):
                            continue
                        low = fn.lower()
                        if not all(w in low or w in low_rel for w in kws):
                            continue
                        try:
                            stt = os.stat(os.path.join(dirpath, fn))
                        except OSError:
                            continue
                        out.append((fn, stt.st_size, False, rel, stt.st_mtime))
                        if len(out) >= 800:
                            self.ui(self._deep_done, "local", gen, out)
                            return
            except Exception as exc:
                self.log(f"电脑侧子目录搜索中断：{exc}", "warn")
            self.ui(self._deep_done, "local", gen, out)

        threading.Thread(target=work, daemon=True).start()

    def _deep_phone(self, base, gen):
        kws = [w for w in self._find["phone"]["kw"].lower().split() if w]

        def work():
            serial = get_serial()
            rows = []
            if serial:
                try:
                    cmd = f"find {shq(base)} -maxdepth 4 -type f 2>/dev/null"
                    out = adb_shell(serial, cmd, timeout=30)
                    if out is None:
                        rc, out = adb(["-s", serial, "shell", cmd], timeout=45)
                    for line in (out or "").splitlines():
                        p = line.strip()
                        if not p.startswith("/") or gen != self._search_gen["phone"]:
                            continue
                        name = p.rsplit("/", 1)[-1]
                        if name.startswith("."):
                            continue
                        relp = p[len(base):].lstrip("/") if p.startswith(base) else p
                        rel = relp.rsplit("/", 1)[0] if "/" in relp else ""
                        low, low_rel = name.lower(), rel.lower()
                        if not all(w in low or w in low_rel for w in kws):
                            continue
                        rows.append((name, -1, False, rel, 0))
                        if len(rows) >= 800:
                            break
                except Exception as exc:
                    self.log(f"手机子目录搜索失败：{str(exc)[:60]}", "warn")
            self.ui(self._deep_done, "phone", gen, rows)

        threading.Thread(target=work, daemon=True).start()

    def _deep_done(self, side, gen, rows):
        if gen != self._search_gen[side]:
            return                                   # 用户又改了词，这批作废
        self._deep_busy[side] = False
        self._rows_deep[side] = rows
        self._paint_rows(side)

    # ---- 缩略图：磁盘缓存 + 后台单线程，绝不在主线程解码图片
    def need_thumb(self, side, name, size):
        """FileGrid 画到某个图片时来要缩略图。缓存命中就当场贴，否则排队。"""
        grid = self.grid_local if side == "local" else self.grid_phone
        if grid.rel_of(name):
            return                    # 子目录命中的文件，路径不在当前目录，不取图
        if side == "local":
            src = os.path.join(self.local_path, name)
            key = f"L|{src}|{size}"
        else:
            src = self.current_path + name
            key = f"P|{src}|{size}"
        png = thumb_cache_path(key)
        if os.path.exists(png) and os.path.getsize(png) > 0:
            grid.set_thumb(name, png)
            return
        cur_dir = self.local_path if side == "local" else self.current_path
        self._thumb_q.put((side, name, src, png, cur_dir))

    def _thumb_loop(self):
        try:
            os.makedirs(THUMB_CACHE, exist_ok=True)
        except OSError:
            return
        while True:
            item = self._thumb_q.get()
            if item is None:
                return
            try:
                side, name, src, png, req_dir = item
                # 用户已经翻到别的目录了：这一张就不做了，省掉一次 adb 传输
                cur = self.local_path if side == "local" else self.current_path
                if cur != req_dir:
                    continue
                if os.path.exists(png) and os.path.getsize(png) > 0:
                    self.ui(self._thumb_ready, side, name, png)
                    continue
                if side == "local":
                    ok = make_thumb(src, png)
                else:
                    ok = self._thumb_from_phone(src, png)
                if ok:
                    self.ui(self._thumb_ready, side, name, png)
            except Exception:
                pass

    def _thumb_from_phone(self, remote, png):
        """手机上的图先取字节再解码。

        用 `exec-out cat` 而不是 `adb pull`：pull 在 Windows 上会把非 ASCII
        文件名弄坏（这也是之前推送失败的根因），cat 的路径走手机 shell，
        中文完全正常，而且输出是二进制安全的。
        """
        serial = get_serial()
        if not serial:
            return False
        tmp = os.path.join(THUMB_CACHE, f"_src_{os.getpid()}_{threading.get_ident()}.tmp")
        try:
            with open(tmp, "wb") as fh:
                subprocess.run([ADB, "-s", serial, "exec-out", "cat", remote],
                               stdout=fh, stderr=subprocess.DEVNULL,
                               timeout=120, **QUIET)
            if not os.path.exists(tmp) or os.path.getsize(tmp) == 0:
                return False
            return make_thumb(tmp, png)
        except Exception:
            return False
        finally:
            try:
                os.remove(tmp)
            except OSError:
                pass

    def _thumb_ready(self, side, name, png):
        grid = self.grid_local if side == "local" else self.grid_phone
        grid.set_thumb(name, png)

    # ---- 浏览与跳转
    def browse(self, path=PHONE_HOME):
        """切换手机侧目录（保留旧接口，启动时也会调用）"""
        path = path.replace("//", "/")
        if not path.startswith("/sdcard"):
            path = PHONE_HOME
        if not path.endswith("/"):
            path += "/"
        self.current_path = path
        self._dir_changed("phone")
        self._scan_phone_async()
        self._mark_place()

    def goto_place(self, path):
        """点「下载与接收 / 相册 / 文档…」直接跳过去"""
        self.browse(path + "/")

    def pane_up(self, side):
        if side == "local":
            p = os.path.dirname(self.local_path.rstrip("\\/"))
            if p and os.path.isdir(p):
                self.local_path = p
                self._dir_changed("local")
                self._scan_local()
        else:
            if self.current_path.rstrip("/") == "/sdcard":
                return
            p = self.current_path.rstrip("/")
            self.browse(p[:p.rfind("/") + 1])

    def pick_local_dir(self):
        d = filedialog.askdirectory(title="选择电脑侧目录", initialdir=self.local_path)
        if d:
            self.local_path = os.path.normpath(d)
            self._dir_changed("local")
            self._scan_local()

    def open_dir(self, name, side, rel=""):
        """进入某一栏里的子目录。rel 是搜索命中时的父路径。"""
        if side == "local":
            p = os.path.join(self.local_path, rel, name) if rel else os.path.join(
                self.local_path, name)
            if os.path.isdir(p):
                self.local_path = p
                self._dir_changed("local")
                self._scan_local()
        else:
            self.browse(self.current_path + (rel + "/" if rel else "") + name + "/")

    def pane_open(self, name, isdir, side):
        grid = self.grid_local if side == "local" else self.grid_phone
        rel = grid.rel_of(name)
        if isdir:
            self.open_dir(name, side, rel)
        else:
            self.transfer(side, [(name, rel)])         # 双击文件＝传给对面

    def _tile_hint(self, text):
        """鼠标停在某个格子上时，底部显示完整文件名和大小。"""
        x, y = self.root.winfo_pointerxy()
        side = "local" if self.grid_local.contains_root(x, y) else "phone"
        hint = self.local_hint if side == "local" else self.phone_hint
        if text:
            hint.config(text=text)
        else:
            st = self._find[side]
            n = len(self.local_entries if side == "local" else self.entries)
            hint.config(text=(f"找到 {n} 项" if (st["kw"] or st["kind"]) else f"{n} 项"))

    # ---- 拖拽（tkinter 没有原生跨控件 DnD，这里自己做按下/移动/释放）
    def _drag_start(self, event, side):
        grid = self.grid_local if side == "local" else self.grid_phone
        name = grid.name_at_root(event.x_root, event.y_root)
        if not name:
            self._drag = None
            return
        rows = grid.selection()
        if name not in rows:
            grid.select_only(name)
            rows = [name]
        self._drag = {"side": side, "rows": rows,
                      "x": event.x_root, "y": event.y_root,
                      "ghost": None, "moved": False}

    def _drag_move(self, event):
        d = self._drag
        if not d:
            return
        if not d["moved"]:
            if (abs(event.x_root - d["x"]) < DRAG_THRESHOLD
                    and abs(event.y_root - d["y"]) < DRAG_THRESHOLD):
                return
            d["moved"] = True
            ghost = tk.Toplevel(self.root)
            ghost.overrideredirect(True)
            try:
                ghost.attributes("-topmost", True)
            except tk.TclError:
                pass
            text = d["rows"][0] if len(d["rows"]) == 1 else f"{len(d['rows'])} 项"
            tk.Label(ghost, text=text, bg="#2b3240", fg="#ffffff",
                     font=F_SMALL, padx=10, pady=4).pack()
            d["ghost"] = ghost
        if d["ghost"] is not None:
            d["ghost"].geometry(f"+{event.x_root + 14}+{event.y_root + 12}")

    def _drag_drop(self, event, side):
        d = self._drag
        self._drag = None
        if not d or not d["moved"]:
            return
        if d["ghost"] is not None:
            d["ghost"].destroy()
        src = d["side"]
        if src == "local":
            grid, rows = self.grid_phone, self.entries
            base = self.current_path
        else:
            grid, rows = self.grid_local, self.local_entries
            base = self.local_path
        if not grid.contains_root(event.x_root, event.y_root):
            return
        # 落在某个文件夹的格子上 → 直接放进那个文件夹；落在空白处 → 当前目录
        name = grid.name_at_root(event.x_root, event.y_root)
        row = next((r for r in rows if r[0] == name), None)
        dest = base
        if row and row[2]:
            dest = (base + name + "/") if src == "local" else os.path.join(base, name)
        src_grid = self.grid_local if src == "local" else self.grid_phone
        self.transfer(src, [(n, src_grid.rel_of(n)) for n in d["rows"]], dest)

    # ---- 传输：side 是源端，local=电脑→手机，phone=手机→电脑
    def transfer(self, side, items, dest_dir=None):
        """items 每一项是名字，或 (名字, 相对当前目录的父路径)。

        子目录搜索结果里的文件并不在当前目录下，只凭名字拼路径会指到别处，
        所以把它的来源相对路径一起带过来。
        """
        if not items:
            return
        norm = [(it, "") if isinstance(it, str) else (it[0], it[1] or "")
                for it in items]
        if side == "local":
            srcs = [os.path.join(self.local_path, rel, n) if rel
                    else os.path.join(self.local_path, n) for n, rel in norm]
            self.bg(self._push_to_phone, srcs, dest_dir or self.current_path)
        else:
            rps = [self.current_path + (rel + "/" if rel else "") + n
                   for n, rel in norm]
            self.bg(self._pull_to_local, rps, [n for n, _ in norm],
                    dest_dir or self.local_path)

    def _pull_to_local(self, remote_paths, names, dest):
        serial = get_serial()
        if not serial:
            self.log("手机未连接，无法拉取", "err")
            return
        try:
            os.makedirs(dest, exist_ok=True)
        except OSError as exc:
            self.log(f"目标目录不可写：{exc}", "err")
            return
        ok = 0
        for i, (rp, n) in enumerate(zip(remote_paths, names)):
            final = os.path.join(dest, n)
            rc_t, typ = adb(["-s", serial, "shell",
                             f"if [ -d {shq(rp)} ]; then echo DIR; else echo FILE; fi"], timeout=30)
            if "DIR" in (typ or ""):
                rc, out = adb(["-s", serial, "pull", rp, final], timeout=1800)
                if os.path.isdir(final):
                    ok += 1
                    self.log(f"已拉到电脑：{final}/（整个文件夹）", "ok")
                else:
                    self.log(f"拉取失败：{n}  {(out or '').strip()[:100]}", "err")
                continue
            # 本地临时名同样用 ASCII：改名这一步交给 Windows 自己做，
            # adb 全程不碰中文，落盘后再换成真名
            tmp = os.path.join(dest, f".wbpull_{i}_{os.getpid()}.part")
            try:
                if os.path.exists(tmp):
                    os.remove(tmp)
            except OSError:
                pass
            self.log(f"正在拉取 {n} …")
            adb(["-s", serial, "pull", rp, tmp], timeout=1800)
            if not os.path.exists(tmp):
                # 中文远端路径偶尔会被 adb 找错：先让手机复制成 ASCII 名再拉
                dev_tmp = f"/sdcard/.wbpull_{i}_{os.getpid()}.part"
                adb(["-s", serial, "shell", f"cp -f {shq(rp)} {shq(dev_tmp)}"], timeout=600)
                adb(["-s", serial, "pull", dev_tmp, tmp], timeout=1800)
                adb(["-s", serial, "shell", f"rm -f {shq(dev_tmp)}"], timeout=60)
            if os.path.exists(tmp):
                got = os.path.getsize(tmp)
                rc_s, stat = adb(["-s", serial, "shell", f"stat -c %s {shq(rp)}"], timeout=30)
                want = (stat or "").strip().splitlines()[-1].strip() if stat.strip() else ""
                try:
                    os.replace(tmp, final)
                except OSError as exc:
                    self.log(f"落盘失败：{n}  {exc}", "err")
                    continue
                ok += 1
                self.log(f"已拉到电脑：{final}（{human_size(got)}）", "ok")
                if want and want.isdigit() and want != str(got):
                    self.log(f"⚠ 大小不一致：远端 {want} 字节，本地 {got} 字节", "warn")
                continue
            self.log(f"拉取失败：{n}", "err")
        self.log(f"拉取完成 {ok}/{len(names)} → {dest}")

    def _push_to_phone(self, local_paths, remote_dir):
        serial = get_serial()
        if not serial:
            self.log("手机未连接，无法推送", "err")
            return
        base = remote_dir.rstrip("/") or "/"
        # 预检目标目录：Android 11 起，别的应用的私有目录对 adb 是「隐藏」的，
        # 直接 push 只会得到含糊的 "remote couldn't create file: Is a directory"，
        # 先在手机端探一次可写性，把话说清楚。
        rc_w, wtest = adb(["-s", serial, "shell",
                           f"if [ -w {shq(base)} ]; then echo WRITABLE; else echo NOWRITE; fi"],
                          timeout=30)
        if "WRITABLE" not in (wtest or ""):
            self.log(f"这个目录不允许写入：{base}/", "err")
            self.log("Android 11 起，其他应用的私有目录对 adb 不可见（系统会伪装成「不存在」）。"
                     "换到 /sdcard/Download/ 或 /sdcard/Documents/ 再拖。", "warn")
            return
        ok = 0
        for i, lp in enumerate(local_paths):
            if not os.path.exists(lp):
                self.log(f"源文件不存在：{lp}", "err")
                continue
            name = os.path.basename(lp)
            if os.path.isdir(lp):
                self.log(f"暂不支持直接拖文件夹：{name}（先压缩成 zip 再拖）", "warn")
                continue
            remote_file = f"{base}/{name}"

            # 远端同名的是文件夹就拒绝（不删你的东西），是文件则交给 mv -f 覆盖
            rc_t, typ = adb(["-s", serial, "shell",
                             f"if [ -d {shq(remote_file)} ]; then echo DIR; else echo FILE; fi"],
                            timeout=30)
            if "DIR" in (typ or ""):
                self.log(f"手机上已有同名文件夹 {remote_file}，不能覆盖，已跳过 {name}", "err")
                continue

            # 关键一步：中文名不进 adb push，先推到纯 ASCII 临时名
            tmp_remote = f"{base}/.wbpush_{i}_{os.getpid()}.part"
            self.log(f"正在推送 {name} …")
            rc, out = adb(["-s", serial, "push", lp, tmp_remote], timeout=1800)

            # 手机端改名 + 回读字节数（同一趟 shell 里完成，省一次 adb 进程）
            cmd = (f"if [ -f {shq(tmp_remote)} ]; then "
                   f"mv -f {shq(tmp_remote)} {shq(remote_file)} && "
                   f"echo MOVED && stat -c %s {shq(remote_file)}; "
                   f"else echo NOSRC; fi")
            rc2, res = adb(["-s", serial, "shell", cmd], timeout=120)
            res = (res or "").replace("\r", "")
            got = ""
            if "MOVED" in res:
                tail = [x.strip() for x in res.splitlines() if x.strip().isdigit()]
                got = tail[-1] if tail else ""
            try:
                want = str(os.path.getsize(lp))
            except OSError:
                want = ""

            if got and got == want:
                ok += 1
                self.log(f"已推到手机：{name} → {remote_file}（{human_size(int(want))}）", "ok")
            else:
                why = "手机端改名失败" if "NOSRC" in res else (res.strip()[:120] or out.strip()[:120])
                self.log(f"推送未生效：{name}｜本地 {want or '?'} 字节，远端 {got or '无此文件'}｜{why}", "err")

        self.log(f"推送完成 {ok}/{len(local_paths)} → {base}/"
                 + ("" if ok == len(local_paths) else "（失败请看上面的原因）"))

    # 旧的「拉到电脑 / 推送文件到手机」按钮已由拖拽取代（见 transfer）

    def screenshot(self):
        self.bg(self._screenshot)

    def _screenshot(self):
        serial = get_serial()
        if not serial:
            self.log("手机未连接", "err")
            return
        dest = os.path.join(BASE, "截图")
        os.makedirs(dest, exist_ok=True)
        name = time.strftime("截图_%Y%m%d_%H%M%S.png")
        p = subprocess.run([ADB, "-s", serial, "exec-out", "screencap", "-p"],
                           capture_output=True, **QUIET)
        if p.stdout.startswith(b"\x89PNG"):
            open(os.path.join(dest, name), "wb").write(p.stdout)
            self.log(f"截图已保存：{name}", "ok")
        else:
            self.log("截图失败", "err")

    def open_recv(self):
        d = os.path.join(BASE, "接收文件")
        os.makedirs(d, exist_ok=True)
        os.startfile(d)

    # ---------------------------------------------------------------- 录制视频
    def toggle_record(self):
        self.bg(self._toggle_record)

    def _screen_size(self, serial):
        rc, out = adb(["-s", serial, "shell", "wm", "size"])
        m = re.search(r"(\d+)x(\d+)", out)
        return (int(m.group(1)), int(m.group(2))) if m else (1080, 2400)

    def _dump_ui(self, serial):
        rc, out = adb(["-s", serial, "shell", "uiautomator", "dump",
                       "/data/local/tmp/hy_ui.xml"], timeout=30)
        if "hierarchy" not in out and "dumped" not in out:
            return None
        rc, xml = adb(["-s", serial, "shell", "cat", "/data/local/tmp/hy_ui.xml"],
                      timeout=30)
        return xml if "<hierarchy" in xml else None

    def _find_record_tile(self, xml):
        """在控制中心 UI 里找「屏幕录制」，返回中心坐标"""
        if not xml:
            return None
        try:
            root = ET.fromstring(xml[xml.find("<hierarchy"):])
        except ET.ParseError:
            return None
        for node in root.iter("node"):
            desc = (node.get("content-desc") or "") + (node.get("text") or "")
            if "屏幕录制" in desc or "录屏" in desc or "Screen record" in desc:
                m = re.match(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", node.get("bounds", ""))
                if m:
                    x1, y1, x2, y2 = map(int, m.groups())
                    return (x1 + x2) // 2, (y1 + y2) // 2
        return None

    def _toggle_record(self):
        serial = get_serial()
        if not serial:
            self.log("手机未连接", "err")
            return

        # 熄屏时控制中心拉不下来，先唤醒
        adb(["-s", serial, "shell", "input", "keyevent", "KEYCODE_WAKEUP"])
        adb(["-s", serial, "shell", "wm", "dismiss-keyguard"])
        time.sleep(0.8)

        w, h = self._screen_size(serial)
        adb(["-s", serial, "shell", "cmd", "statusbar", "expand-settings"])
        time.sleep(1.5)

        xml = self._dump_ui(serial)
        if not xml or "控制中心" not in xml:
            # 锁屏或有密码时拉不下控制中心；不要在桌面上乱滑，避免误触
            self.log("打不开手机控制中心，请先解锁手机再试", "err")
            return

        pos = self._find_record_tile(xml)
        if not pos:
            # 快捷开关默认只显示一行，向下滑展开完整面板
            adb(["-s", serial, "shell", "input", "swipe", str(w // 2), str(int(h * 0.39)),
                 str(w // 2), str(int(h * 0.66)), "400"])
            time.sleep(0.8)
            pos = self._find_record_tile(self._dump_ui(serial))
        if not pos:
            self.log("控制中心里没找到「屏幕录制」开关，请确认该开关已添加到控制中心", "err")
            adb(["-s", serial, "shell", "cmd", "statusbar", "collapse"])
            return

        adb(["-s", serial, "shell", "input", "tap", str(pos[0]), str(pos[1])])
        time.sleep(1.5)
        self.recording = not self.recording
        self.ui(self._apply_rec_state)
        if self.recording:
            self.log("开始录制视频（手机控制中心录屏）", "ok")
        else:
            self.log("已停止录制，视频已存到手机相册 Pictures/Screenshots", "ok")

    def _apply_rec_state(self):
        self.rec_btn.configure(text="停止录制" if self.recording else "录制视频")

def main():
    root = tk.Tk()
    app = App(root)
    # 关窗口时回收常驻 adb shell，否则会留下孤儿 adb.exe 进程
    root.protocol("WM_DELETE_WINDOW",
                  lambda: (drop_adb_shell(), root.destroy()))
    try:
        root.mainloop()
    finally:
        drop_adb_shell()


if __name__ == "__main__":
    main()
