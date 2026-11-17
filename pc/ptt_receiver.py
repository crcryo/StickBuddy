"""
StickBuddy —— StickS3 语音键接收端
==================
按键含义（全部在电脑端定义，改手感不用重刷固件）：

  正面键 按住 > 700ms → 录音；松开 → 智谱转文字 → 打进当前光标
  正面键 单击        → 发送 Enter
  侧键   单击        → 唤出 WorkBuddy   长按 → Esc

声音反馈（不用看设备屏幕）：
  1 声短"嘀"      = 开始录音
  2 声短"嘀"      = 识别完成，正在打字
  1 声低"嘟"      = 出错 / 没听清

设备屏幕状态（需配套固件，旧固件会自动忽略）：
  红点   = 录音中    琥珀色闪烁 = 转写中
  绿色勾 = 完成      红色叉 = 出错

依赖：bleak sounddevice requests  （pip install bleak sounddevice requests）
"""

import os
import sys as _sys

# 无窗口(--windowed)模式下 stdout/stderr 是 None，先接到空设备，
# 否则任何 print 都会抛异常导致程序静默退出。
if _sys.stdout is None:
    _sys.stdout = open(os.devnull, "w", encoding="utf-8")
if _sys.stderr is None:
    _sys.stderr = open(os.devnull, "w", encoding="utf-8")

# ---------- 安装根目录：onedir 模式下 exe 在 app\StickBuddy\ 里，日志/配置放安装根 ----------
def _app_root():
    d = (os.path.dirname(_sys.executable) if getattr(_sys, "frozen", False)
         else os.path.dirname(os.path.abspath(__file__)))
    if os.path.isdir(os.path.join(d, "_internal")):   # PyInstaller 6 onedir
        d = os.path.abspath(os.path.join(d, "..", ".."))
    return d

APP_DIR = _app_root()

# ---------- 崩溃追踪：任何原生崩溃/未捕获异常都写入 crash.log（排查静默退出用） ----------
try:
    import time as _time
    import threading as _threading
    import faulthandler

    _CRASH_F = open(os.path.join(APP_DIR, "crash.log"), "a", encoding="utf-8")
    _CRASH_F.write("\n===== %s 进程启动 PID=%s =====\n" % (_time.strftime("%F %T"), os.getpid()))
    faulthandler.enable(_CRASH_F)

    def _excepthook(t, v, tb):
        import traceback
        _CRASH_F.write("未捕获异常:\n" + "".join(traceback.format_exception(t, v, tb)) + "\n")
        _CRASH_F.flush()
        _sys.__excepthook__(t, v, tb)

    _sys.excepthook = _excepthook
    _threading.excepthook = lambda a: _excepthook(a.exc_type, a.exc_value, a.exc_traceback)
except Exception:
    pass

import ctypes

# 必须在导入 sounddevice 之前把 COM 初始化为多线程套间(MTA)，
# 否则 PortAudio 会把主线程设成 GUI/STA，bleak 会拒绝工作。
try:
    ctypes.windll.ole32.CoInitializeEx(None, 0x0)  # COINIT_MULTITHREADED
except Exception:
    pass

import asyncio
import io
import threading
import time
import sys
import wave
from ctypes import wintypes

import requests
import sounddevice as sd
from bleak import BleakClient, BleakScanner

# ---------------- 配置 ----------------
# 新固件改名 StickBuddy；旧固件仍叫 StickS3-PTT，两者都认
DEVICE_NAMES = ("StickBuddy", "StickS3-PTT")
TX_UUID = "6b1e0002-5f2a-4c8e-9d3b-1a2b3c4d5e6f"
RX_UUID = "6b1e0003-5f2a-4c8e-9d3b-1a2b3c4d5e6f"

CONFIG_PATH = os.path.join(APP_DIR, "config.json")


def _load_zhipu_key():
    """API Key 放 config.json，不放源码（要开源发布）。
    首次运行自动生成模板，提示用户去智谱 open.bigmodel.cn 申请 Key。"""
    import json
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            k = ((json.load(f) or {}).get("zhipu_key") or "").strip()
        if k and "在这里" not in k:
            return k
    except Exception:
        pass
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump({"zhipu_key": "在这里粘贴你的智谱API Key（open.bigmodel.cn 免费申请）"},
                      f, ensure_ascii=False, indent=2)
    except Exception:
        pass
    return ""


ZHIPU_KEY = _load_zhipu_key()
ASR_URL = "https://open.bigmodel.cn/api/paas/v4/audio/transcriptions"
ASR_MODEL = "glm-asr-2512"

CLICK_MS = 700          # 短于这个时长算"单击"→ Enter（人手点一下约 400~600ms）
MIN_RECORD_MS = 900     # 录音短于这个时长视为误触，既不发回车也不转写
MAX_RECORD_S = 28       # 智谱 ASR 上限 30 秒，留 2s 余量自动截断，防止静默失败
# --------------------------------------

def _pick_rate():
    """优先用设备原生 48000，避免 MME 重采样带来的音质损耗。"""
    dev = sd.query_devices(kind="input")
    idx = dev["index"]
    for r in (48000, 44100, 16000):
        try:
            sd.check_input_settings(device=idx, samplerate=r,
                                    channels=1, dtype="int16")
            return r
        except Exception:
            continue
    return int(dev["default_samplerate"] or 48000)


SAMPLE_RATE = _pick_rate()

# ---------- 声音反馈 ----------
try:
    import winsound

    def beep_start():
        winsound.Beep(1400, 45)

    def beep_ok():
        winsound.Beep(1600, 50)
        time.sleep(0.07)
        winsound.Beep(2100, 50)

    def beep_err():
        winsound.Beep(420, 180)
except Exception:  # 非 Windows 兜底
    def beep_start(): pass
    def beep_ok(): pass
    def beep_err(): pass


# ---------- 日志（无窗口模式下唯一的排查入口） ----------
LOG_PATH = os.path.join(APP_DIR, "stickbuddy.log")

try:
    if os.path.exists(LOG_PATH) and os.path.getsize(LOG_PATH) > 1024 * 1024:
        open(LOG_PATH, "w", encoding="utf-8").close()
except Exception:
    pass


def log(msg):
    line = "[%s] %s" % (time.strftime("%H:%M:%S"), msg)
    print(line, flush=True)
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(time.strftime("%Y-%m-%d ") + line + "\n")
    except Exception:
        pass


# ---------- 键盘输入 ----------
user32 = ctypes.windll.user32

INPUT_KEYBOARD = 1
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004

# ULONG_PTR：x64 下 8 字节，x86 下 4 字节
ULONG_PTR = ctypes.c_ulonglong if ctypes.sizeof(ctypes.c_void_p) == 8 else ctypes.c_ulong


class MOUSEINPUT(ctypes.Structure):
    """必须定义：它才是 union 里最大的成员，决定 INPUT 总长度为 40 字节。
    少了它，sizeof(INPUT)=32，SendInput 会静默拒绝所有事件。"""
    _fields_ = [("dx", wintypes.LONG),
                ("dy", wintypes.LONG),
                ("mouseData", wintypes.DWORD),
                ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD),
                ("dwExtraInfo", ULONG_PTR)]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wintypes.WORD),
                ("wScan", wintypes.WORD),
                ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD),
                ("dwExtraInfo", ULONG_PTR)]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [("uMsg", wintypes.DWORD),
                ("wParamL", wintypes.WORD),
                ("wParamH", wintypes.WORD)]


class INPUT(ctypes.Structure):
    class _I(ctypes.Union):
        _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]

    _anonymous_ = ("i",)
    _fields_ = [("type", wintypes.DWORD), ("i", _I)]


user32.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int]
user32.SendInput.restype = wintypes.UINT

_INPUT_SIZE = ctypes.sizeof(INPUT)


def _send(*inputs):
    arr = (INPUT * len(inputs))(*inputs)
    n = user32.SendInput(len(inputs), arr, _INPUT_SIZE)
    if n != len(inputs):
        log(f"  SendInput 只发出 {n}/{len(inputs)} 个事件（结构体 {_INPUT_SIZE} 字节）")
    return n == len(inputs)


def press_key(vk):
    down = INPUT(type=INPUT_KEYBOARD, ki=KEYBDINPUT(vk, 0, 0, 0, 0))
    up = INPUT(type=INPUT_KEYBOARD, ki=KEYBDINPUT(vk, 0, KEYEVENTF_KEYUP, 0, 0))
    _send(down, up)


def type_text(text):
    """逐 UTF-16 码元注入，兼容中文/emoji，不经过输入法"""
    buf = text.encode("utf-16-le")
    ok = 0
    for i in range(0, len(buf), 2):
        scan = int.from_bytes(buf[i:i + 2], "little")
        down = INPUT(type=INPUT_KEYBOARD,
                     ki=KEYBDINPUT(0, scan, KEYEVENTF_UNICODE, 0, 0))
        up = INPUT(type=INPUT_KEYBOARD,
                   ki=KEYBDINPUT(0, scan, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP, 0, 0))
        if _send(down, up):
            ok += 1
        time.sleep(0.003)
    return ok


# ---------- 文字注入（剪贴板优先，键盘注入兜底） ----------
user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002
VK_CONTROL = 0x11
VK_V = 0x56

# 关键：ctypes 默认把返回值当 32 位 int。x64 下剪贴板句柄是 64 位指针，
# 不声明 restype 会被截断，SetClipboardData 收到无效句柄必然失败。
user32.OpenClipboard.argtypes = [ctypes.c_void_p]
user32.OpenClipboard.restype = wintypes.BOOL
user32.CloseClipboard.argtypes = []
user32.CloseClipboard.restype = wintypes.BOOL
user32.EmptyClipboard.argtypes = []
user32.EmptyClipboard.restype = wintypes.BOOL
user32.GetClipboardData.argtypes = [wintypes.UINT]
user32.GetClipboardData.restype = ctypes.c_void_p
user32.SetClipboardData.argtypes = [wintypes.UINT, ctypes.c_void_p]
user32.SetClipboardData.restype = ctypes.c_void_p

kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
kernel32.GlobalAlloc.restype = ctypes.c_void_p
kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
kernel32.GlobalLock.restype = ctypes.c_void_p
kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
kernel32.GlobalUnlock.restype = wintypes.BOOL
kernel32.GlobalFree.argtypes = [ctypes.c_void_p]
kernel32.GlobalFree.restype = ctypes.c_void_p


def foreground_title():
    try:
        hwnd = user32.GetForegroundWindow()
        n = user32.GetWindowTextLengthW(hwnd)
        if n <= 0:
            return "(无标题)"
        buf = ctypes.create_unicode_buffer(n + 1)
        user32.GetWindowTextW(hwnd, buf, n + 1)
        return buf.value
    except Exception:
        return "(未知)"


def _clip_get():
    opened = False
    for _ in range(6):
        if user32.OpenClipboard(None):
            opened = True
            break
        time.sleep(0.04)
    if not opened:
        return None
    try:
        h = user32.GetClipboardData(CF_UNICODETEXT)
        if not h:
            return None
        p = kernel32.GlobalLock(h)
        if not p:
            return None
        try:
            return ctypes.wstring_at(p)
        finally:
            kernel32.GlobalUnlock(h)
    except Exception:
        return None
    finally:
        user32.CloseClipboard()


def _clip_set(text):
    data = text.encode("utf-16-le") + b"\x00\x00"
    opened = False
    for _ in range(6):
        if user32.OpenClipboard(None):
            opened = True
            break
        time.sleep(0.04)
    if not opened:
        log("  剪贴板被其他程序占用，打不开")
        return False
    try:
        user32.EmptyClipboard()
        h = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(data))
        if not h:
            log("  GlobalAlloc 失败")
            return False
        p = kernel32.GlobalLock(h)
        if not p:
            kernel32.GlobalFree(h)
            log("  GlobalLock 失败")
            return False
        ctypes.memmove(p, data, len(data))
        kernel32.GlobalUnlock(h)
        if not user32.SetClipboardData(CF_UNICODETEXT, h):
            kernel32.GlobalFree(h)
            log("  SetClipboardData 失败")
            return False
        return True
    except Exception as e:
        log(f"  剪贴板异常: {e}")
        return False
    finally:
        user32.CloseClipboard()


def _ctrl_v():
    _send(
        INPUT(type=INPUT_KEYBOARD, ki=KEYBDINPUT(VK_CONTROL, 0, 0, 0, 0)),
        INPUT(type=INPUT_KEYBOARD, ki=KEYBDINPUT(VK_V, 0, 0, 0, 0)),
        INPUT(type=INPUT_KEYBOARD, ki=KEYBDINPUT(VK_V, 0, KEYEVENTF_KEYUP, 0, 0)),
        INPUT(type=INPUT_KEYBOARD, ki=KEYBDINPUT(VK_CONTROL, 0, KEYEVENTF_KEYUP, 0, 0)),
    )


def paste_text(text):
    """写剪贴板 → Ctrl+V → 还原原剪贴板内容"""
    old = _clip_get()
    if not _clip_set(text):
        return False
    time.sleep(0.08)
    _ctrl_v()
    time.sleep(0.22)
    if old is not None:
        _clip_set(old)
    return True


def inject_text(text):
    log("注入时前台窗口: " + foreground_title())
    if paste_text(text):
        log("注入方式: 剪贴板粘贴 ✅")
    else:
        log("剪贴板不可用，改用键盘注入")
        n = type_text(text)
        log(f"键盘注入完成，共 {n} 个字符")


# ---------- 窗口激活：把 WorkBuddy 拉到前台 ----------
WINDOW_KEYWORD = "WorkBuddy"


def activate_window(keyword=WINDOW_KEYWORD):
    """找到标题含 keyword 的可见窗口并置前，成功返回 True。"""
    user32 = ctypes.windll.user32
    found = []
    EnumProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def _cb(hwnd, _):
        try:
            if not user32.IsWindowVisible(hwnd):
                return True
            n = user32.GetWindowTextLengthW(hwnd)
            if n == 0:
                return True
            buf = ctypes.create_unicode_buffer(n + 1)
            user32.GetWindowTextW(hwnd, buf, n + 1)
            if keyword.lower() in buf.value.lower():
                found.append(hwnd)
        except Exception:
            pass
        return True

    user32.EnumWindows(EnumProc(_cb), 0)
    if not found:
        return False

    hwnd = found[0]
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, 9)          # SW_RESTORE
    # 按一下 Alt 绕过 Windows 的前台窗口锁定
    user32.keybd_event(0x12, 0, 0, 0)
    ok = user32.SetForegroundWindow(hwnd)
    user32.keybd_event(0x12, 0, 2, 0)
    return bool(ok)


# ---------- 系统托盘 ----------
def _tray_image():
    from PIL import Image, ImageDraw
    s = 64
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse((1, 1, s - 2, s - 2), fill=(30, 110, 210, 255))
    d.rounded_rectangle((26, 12, 38, 36), radius=6, fill=(255, 255, 255, 255))
    d.arc((19, 24, 45, 46), start=0, end=180, fill=(255, 255, 255, 255), width=3)
    d.line((32, 46, 32, 53), fill=(255, 255, 255, 255), width=3)
    d.line((24, 54, 40, 54), fill=(255, 255, 255, 255), width=3)
    return img


def run_tray():
    import pystray
    from pystray import MenuItem as Item

    def on_focus(icon=None, item=None):
        log("托盘 → 唤出 WorkBuddy")
        if not activate_window():
            beep_err()

    def on_log(icon=None, item=None):
        try:
            os.startfile(LOG_PATH)
        except Exception:
            pass

    def on_quit(icon=None, item=None):
        log("托盘 → 退出")
        try:
            icon.stop()
        except Exception:
            pass
        os._exit(0)

    menu = pystray.Menu(
        Item("唤出 WorkBuddy", on_focus, default=True),
        Item("查看日志", on_log),
        pystray.Menu.SEPARATOR,
        Item("退出", on_quit),
    )
    icon = pystray.Icon("stickbuddy", _tray_image(), "StickBuddy", menu)

    def _setup(ic):
        try:
            ic.visible = True
            ic.notify("StickBuddy 已启动。按住正面键说话，侧键唤出 WorkBuddy。",
                      "StickBuddy")
        except Exception:
            pass

    icon.run(setup=_setup)


# ---------- 录音 ----------
class Recorder:
    def __init__(self):
        self.frames = []
        self.stream = None
        self.lock = threading.Lock()
        self.active = False
        self.capped = False
        self.started_at = 0.0

    def start(self):
        with self.lock:
            self.frames = []
            self.active = True
            self.capped = False
            self.started_at = time.time()

        def cb(indata, frames, t, status):
            with self.lock:
                if not self.active:
                    return
                # 达到智谱 30s 上限前主动截断（留 2s 余量），已录部分照常转写
                if time.time() - self.started_at >= MAX_RECORD_S:
                    self.active = False
                    self.capped = True
                    threading.Thread(target=beep_ok, daemon=True).start()
                    return
                self.frames.append(bytes(indata))

        try:
            self.stream = sd.RawInputStream(samplerate=SAMPLE_RATE, channels=1,
                                            dtype="int16", blocksize=800, callback=cb)
            self.stream.start()
        except Exception as e:
            log(f"录音启动失败: {e}")
            self.active = False

    def stop(self):
        with self.lock:
            self.active = False
            if self.stream is not None:
                try:
                    self.stream.stop()
                    self.stream.close()
                except Exception:
                    pass
                self.stream = None
            data = b"".join(self.frames)
            self.frames = []
        return data


recorder = Recorder()


def wav_bytes(pcm):
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SAMPLE_RATE)
        w.writeframes(pcm)
    return buf.getvalue()


def transcribe(pcm):
    if not ZHIPU_KEY:
        log("未配置 API Key：请编辑安装目录下的 config.json 后重启 StickBuddy")
        return ""
    files = {"file": ("speech.wav", wav_bytes(pcm), "audio/wav")}
    r = requests.post(ASR_URL, files=files, data={"model": ASR_MODEL},
                      headers={"Authorization": f"Bearer {ZHIPU_KEY}"}, timeout=60)
    if r.status_code != 200:
        log(f"转写失败 HTTP {r.status_code}: {r.text[:200]}")
        return ""
    return (r.json().get("text") or "").strip()


# ---------- 按键处理 ----------
front_down_at = 0.0
front_active = False
side_down_at = 0.0
_status_client = None     # 当前 BLE 连接，用于把状态下发到设备屏幕


def _send_status(tag):
    """把状态（ST:REC/THINK/DONE/ERR/IDLE）写到设备 RX 通道，驱动屏幕显示。
    旧固件不认识这些命令，会安全忽略。"""
    global _status_client
    c = _status_client
    if c is None:
        return
    data = tag.encode("utf-8")

    async def _w():
        try:
            await c.write_gatt_char(RX_UUID, data, response=False)
        except Exception as e1:
            # 旧版 bleak (<=0.13) 的接口名不同，做一次兼容回退
            try:
                await c.write_gatt_attr(RX_UUID, data)
            except Exception as e2:
                log(f"状态发送失败 {tag}: {e1} / {e2}")

    try:
        asyncio.get_running_loop().create_task(_w())
    except Exception:
        pass


def handle(line):
    global front_down_at, front_active, side_down_at
    line = line.strip()
    if not line:
        return

    if line.startswith("F:1"):
        front_down_at = time.time()
        front_active = True
        recorder.start()
        beep_start()
        return

    if line.startswith("F:0"):
        if not front_active:
            return
        front_active = False
        held = int((time.time() - front_down_at) * 1000)
        time.sleep(0.28)          # 尾部缓冲：避免最后一个字被切掉
        pcm = recorder.stop()

        if held < CLICK_MS:
            log(f"单击 {held}ms → Enter")
            beep_ok()
            press_key(0x0D)
            _send_status("ST:DONE")
            return

        if held < MIN_RECORD_MS or len(pcm) < SAMPLE_RATE * 2 * MIN_RECORD_MS // 1000:
            log(f"录音过短 {held}ms，忽略")
            _send_status("ST:IDLE")
            return

        if recorder.capped:
            log(f"已达 {MAX_RECORD_S}s 上限自动截断（智谱限 30s），要继续说请再按一段")
        log(f"录音 {len(pcm) / (SAMPLE_RATE * 2):.1f}s（按住 {held}ms），转写中…")
        _send_status("ST:THINK")
        text = transcribe(pcm)
        if text:
            log(f"→ {text}")
            beep_ok()
            time.sleep(0.05)
            inject_text(text)
            _send_status("ST:DONE")
        else:
            log("没听清，无结果")
            beep_err()
            _send_status("ST:ERR")
        return

    if line.startswith("S:1"):
        side_down_at = time.time()
        return

    if line.startswith("S:0"):
        held_side = int((time.time() - side_down_at) * 1000) if side_down_at else 0
        if held_side >= 600:
            log(f"侧键长按 {held_side}ms → Esc")
            press_key(0x1B)
        else:
            log(f"侧键单击 {held_side}ms → 唤出 WorkBuddy")
            if activate_window():
                beep_ok()
                _send_status("ST:DONE")
            else:
                log("没找到 WorkBuddy 窗口")
                beep_err()
                _send_status("ST:ERR")
        return


def on_notify(char, data):
    try:
        handle(data.decode("utf-8", "replace"))
    except Exception as e:
        log(f"处理异常: {e}")


# ---------- 蓝牙主循环 ----------
async def find_device():
    while True:
        devs = await BleakScanner.discover(timeout=3.0)
        for d in devs:
            if d.name and any(n.lower() in d.name.lower() for n in DEVICE_NAMES):
                return d
        await asyncio.sleep(1)


async def run():
    global _status_client
    while True:
        dev = await find_device()
        log(f"找到 {dev.name} {dev.address}，连接中…")
        try:
            async with BleakClient(dev.address, timeout=25) as client:
                _status_client = client
                await client.start_notify(TX_UUID, on_notify)
                log("已连接 —— 按住正面键说话，松开即出字")
                while client.is_connected:
                    await asyncio.sleep(1)
                log("连接断开，重新搜索…")
        except Exception as e:
            log(f"连接失败: {e}")
        finally:
            _status_client = None
        await asyncio.sleep(1)


_MUTEX = None


def ensure_single_instance():
    """同一时间只允许一个实例，避免多实例互相抢蓝牙连接。"""
    global _MUTEX
    try:
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        _MUTEX = k32.CreateMutexW(None, False, "Global\\StickBuddy")
        return ctypes.get_last_error() != 183      # ERROR_ALREADY_EXISTS
    except Exception:
        return True


def _heartbeat_thread():
    """每 15 分钟写一行存活心跳，方便事后判断程序何时静默退出。"""
    while True:
        time.sleep(900)
        log("[alive] 心跳")


def _ble_thread():
    try:
        ctypes.windll.ole32.CoInitializeEx(None, 0x0)
    except Exception:
        pass
    try:
        asyncio.run(run())
    except Exception as e:
        log(f"BLE 线程异常: {e}")


if __name__ == "__main__":
    if not ensure_single_instance():
        try:
            ctypes.windll.user32.MessageBoxW(
                None,
                "StickBuddy 已经在后台运行了。\n请看屏幕右下角的蓝色托盘图标。",
                "StickBuddy", 0x40)
        except Exception:
            pass
        sys.exit(0)

    log("=" * 46)
    log(" StickBuddy 启动")
    log(" 正面键  按住=说话   单击=回车")
    log(" 侧键    单击=唤出 WorkBuddy   长按=Esc")
    log(" 右下角托盘图标：右键可查看日志 / 退出")
    log("=" * 46)

    threading.Thread(target=_ble_thread, daemon=True).start()
    threading.Thread(target=_heartbeat_thread, daemon=True).start()

    # 托盘循环退出后自动重建：icon.run() 在 explorer 重启等情况下会自行返回，
    # 不重建的话主线程会直接结束、进程静默退出（曾经发生过多次）。
    while True:
        try:
            run_tray()
            log("⚠️ 托盘循环意外返回，3 秒后重建")
        except Exception as e:
            log(f"托盘异常: {e}")
        time.sleep(3)
