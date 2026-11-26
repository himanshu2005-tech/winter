import base64
import os
import threading
import win32gui
import win32api
import uiautomation as auto
from io import BytesIO
from PIL import Image, ImageGrab, ImageDraw, ImageFont, ImageChops
import time
import cv2

# Reduce CUDA fragmentation so OmniParser and OCR don't fight over VRAM
os.environ.setdefault('PYTORCH_CUDA_ALLOC_CONF', 'expandable_segments:True')

# Global lock to prevent GPU memory conflicts when recall worker and main agent use vision concurrently
_vision_lock = threading.Lock()

SEND_MAX_W = 1366
_send_scale = 1.0

def get_send_scale():
    return _send_scale

def _downscale_for_model(img):
    """Returns (downscaled_PIL_image, scale_factor). scale_factor is
    send_width / original_width, i.e. <= 1.0. Coordinates the model
    returns must be DIVIDED by this scale_factor to map back to the
    original/native resolution before clicking."""
    w, h = img.size
    if w <= SEND_MAX_W:
        return img, 1.0
    scale = SEND_MAX_W / w
    return img.resize((SEND_MAX_W, int(h * scale)), Image.LANCZOS), scale

def _screenshot_visually_changed(b64_before, b64_after, threshold=0.015):
    """
    Returns True if the two screenshots differ meaningfully. False is a
    strong signal that a CLICK/CLICK_ID had no visible effect - i.e. it
    missed its target - which is exactly the case that let the model
    hallucinate 'a new form opened' while looking at the same screen.
    """
    if not b64_before or not b64_after:
        return True  # can't compare - fail open, don't block progress
    try:
        img_a = Image.open(BytesIO(base64.b64decode(b64_before))).convert("L").resize((320, 180))
        img_b = Image.open(BytesIO(base64.b64decode(b64_after))).convert("L").resize((320, 180))
        diff = ImageChops.difference(img_a, img_b)
        hist = diff.histogram()
        significant = sum(hist[15:])   # pixels that changed meaningfully
        total = sum(hist)
        return (significant / total) > threshold
    except Exception as e:
        print(f"[Vision Diff] Comparison failed: {e}")
        return True

def _fast_screenshot_b64():
    """Takes a raw screenshot very quickly for stability checking,
    without running OmniParser, OCR, or building the element map."""
    img = _grab_active_window() if CAPTURE_ACTIVE_WINDOW_ONLY else _grab_full_desktop()
    if not img:
        img = ImageGrab.grab()
    img = img.resize((320, 180))
    buf = BytesIO()
    img.save(buf, "JPEG", quality=50)
    return base64.b64encode(buf.getvalue()).decode()

def wait_for_stable(base_dir, max_wait=3.0, poll=0.25, threshold=0.008, return_full=True):
    """
    Polls fast screenshots until two consecutive captures are visually
    near-identical, or max_wait elapses.
    If return_full=True (default), returns the final (b64, element_map_text)
    from a full take_screenshot_b64 call.
    If return_full=False, returns (small_b64, "") to skip heavy processing.
    """
    import time
    prev_b64 = _fast_screenshot_b64()
    time.sleep(poll)
    waited = poll
    while waited < max_wait:
        curr_b64 = _fast_screenshot_b64()
        if not _screenshot_visually_changed(prev_b64, curr_b64, threshold=threshold):
            break
        prev_b64 = curr_b64
        time.sleep(poll)
        waited += poll
        
    if return_full:
        return take_screenshot_b64(base_dir)
    else:
        return (curr_b64 if 'curr_b64' in locals() else prev_b64), ""

# ------------------------------------------------------------------ #
# VIRTUAL-DESKTOP COORDINATE SYSTEM
#
# Windows always puts the PRIMARY monitor's origin at (0, 0). Any
# monitor placed to the LEFT of or ABOVE the primary one therefore has
# NEGATIVE coordinates in the real (virtual-desktop) coordinate space
# that win32api.SetCursorPos / UIA's BoundingRectangle / GetWindowRect
# all use.
#
# Fix: always grab the FULL virtual desktop (ImageGrab.grab(all_screens
# =True)), and explicitly track the origin of that capture so any code
# translating a pixel coordinate on the image back into a real
# SetCursorPos coordinate does so correctly, even when the origin is
# negative.
#
# The model itself keeps working in *image-relative* pixel coordinates
# (i.e. exactly what it sees drawn in the screenshot, top-left = 0,0).
# actions.py is responsible for adding the capture origin back on
# before calling SetCursorPos.
# ------------------------------------------------------------------ #

SM_XVIRTUALSCREEN = 76
SM_YVIRTUALSCREEN = 77
SM_CXVIRTUALSCREEN = 78
SM_CYVIRTUALSCREEN = 79

_capture_origin = (0, 0)
_capture_size = (1920, 1080)
_capture_hwnd = None
_capture_title = ""
_last_user_hwnd = None


def get_capture_source() -> str:
    """Human-readable source of the exact window in the latest screenshot."""
    return _capture_title or "Unknown active window"


def get_virtual_screen_bounds():
    try:
        vx = win32api.GetSystemMetrics(SM_XVIRTUALSCREEN)
        vy = win32api.GetSystemMetrics(SM_YVIRTUALSCREEN)
        vw = win32api.GetSystemMetrics(SM_CXVIRTUALSCREEN)
        vh = win32api.GetSystemMetrics(SM_CYVIRTUALSCREEN)
        if vw > 0 and vh > 0:
            return vx, vy, vw, vh
    except Exception as e:
        print(f"[Vision] GetSystemMetrics virtual screen failed: {e}")
    img = ImageGrab.grab()
    return 0, 0, img.size[0], img.size[1]


def get_capture_origin():
    return _capture_origin


def get_capture_size():
    return _capture_size


def _grab_full_desktop():
    global _capture_origin, _capture_size, _capture_hwnd, _capture_title
    vx, vy, vw, vh = get_virtual_screen_bounds()
    try:
        img = ImageGrab.grab(all_screens=True).convert("RGB")
    except TypeError:
        print("[Vision] Pillow build lacks all_screens=True support; "
              "secondary monitors will NOT be captured. Consider upgrading Pillow.")
        img = ImageGrab.grab().convert("RGB")
        vx, vy = 0, 0
        vw, vh = img.size

    _capture_origin = (vx, vy)
    _capture_size = img.size
    _capture_hwnd = None
    _capture_title = "Desktop fallback"
    return img

CAPTURE_ACTIVE_WINDOW_ONLY = True

# Set of HWNDs that belong to VANTA's own UI.
# ui.py registers its windows here after startup.
_WINTER_HWNDS: set = set()

def register_winter_hwnd(hwnd: int):
    """Called by ui.py to register each of VANTA's own window handles."""
    if hwnd:
        _WINTER_HWNDS.add(hwnd)

# Title substrings that indicate a VANTA / editor window
_PROTECTED_TITLES_LOWER = [
    "vanta",        # "VANTA Ambient", "VANTA Console"
    "ctktoplevel",   # CTk vision preview (generic fallback title)
    "agent.py", "vision.py", "actions.py", "ui.py",
    "antigravity ide", "visual studio code", " - vscode",
    " - notepad++", "pycharm", "sublime text",
]

# Win32 window class names used by Tk / CustomTkinter
_TK_CLASS_PREFIXES = ("tk", "toplevel")


def _is_winter_hwnd(hwnd: int) -> bool:
    """Return True if this HWND belongs to VANTA's own UI overlay."""
    import win32gui
    # Fast path: explicitly registered handle
    if hwnd in _WINTER_HWNDS:
        return True
    try:
        title = win32gui.GetWindowText(hwnd)
        cls   = win32gui.GetClassName(hwnd).lower()
    except Exception:
        return False  # Can't interrogate → assume not ours

    # Tk/CTk windows always have class "Tk" or start with "Tk"
    if cls.startswith(_TK_CLASS_PREFIXES):
        return True

    # Empty title → almost certainly an overlay / splash
    if not title.strip():
        return True

    tl = title.lower()
    return any(sub in tl for sub in _PROTECTED_TITLES_LOWER)


def _grab_active_window():
    """Capture the topmost visible non-VANTA window.

    Strategy:
    1. Try GetForegroundWindow first — fastest and most accurate.
    2. If that window is VANTA's own UI, walk down Z-order with GetWindow
       to find the first real user app.
    3. Fall back to full desktop if nothing usable is found.
    """
    global _capture_origin, _capture_size, _capture_hwnd, _capture_title, _last_user_hwnd
    import win32gui, win32con

    def _print_window(hwnd, width, height):
        """Ask the target window to render itself, avoiding VANTA windows
        that happen to be layered above it on the physical desktop."""
        try:
            import ctypes
            import win32ui

            hwnd_dc = win32gui.GetWindowDC(hwnd)
            src_dc = win32ui.CreateDCFromHandle(hwnd_dc)
            mem_dc = src_dc.CreateCompatibleDC()
            bitmap = win32ui.CreateBitmap()
            bitmap.CreateCompatibleBitmap(src_dc, width, height)
            mem_dc.SelectObject(bitmap)
            # PW_RENDERFULLCONTENT (2) works for Chromium and other modern apps.
            ok = ctypes.windll.user32.PrintWindow(hwnd, mem_dc.GetSafeHdc(), 2)
            info = bitmap.GetInfo()
            bits = bitmap.GetBitmapBits(True)
            image = Image.frombuffer(
                "RGB", (info["bmWidth"], info["bmHeight"]), bits,
                "raw", "BGRX", 0, 1,
            )
            win32gui.DeleteObject(bitmap.GetHandle())
            mem_dc.DeleteDC()
            src_dc.DeleteDC()
            win32gui.ReleaseDC(hwnd, hwnd_dc)
            if not ok:
                return None
            # Reject the common failed PrintWindow result (nearly all black).
            extrema = image.convert("L").resize((64, 64)).getextrema()
            if extrema and extrema[1] <= 8:
                return None
            return image
        except Exception as e:
            print(f"[Vision] Unobscured window capture failed: {e}")
            return None

    def _try_capture(hwnd):
        """Try to capture hwnd. Returns PIL image or None."""
        if not hwnd or not win32gui.IsWindowVisible(hwnd):
            return None
        if win32gui.IsIconic(hwnd):
            return None
        if _is_winter_hwnd(hwnd):
            return None
        r = win32gui.GetWindowRect(hwnd)
        w, h = r[2] - r[0], r[3] - r[1]
        if w < 200 or h < 100:
            return None
        title = win32gui.GetWindowText(hwnd)
        if not title.strip():
            return None
        try:
            import time
            img = _print_window(hwnd, w, h)
            if img is None:
                img = ImageGrab.grab(bbox=(r[0], r[1], r[2], r[3])).convert("RGB")
            print(f"[Vision] Capturing window: '{title}' at {r}")
            return img, (r[0], r[1])
        except Exception as e:
            print(f"[Vision] Capture of '{title}' failed: {e}")
            return None

    # 1. Try foreground window
    fg = win32gui.GetForegroundWindow()
    result = _try_capture(fg)
    if result:
        img, origin = result
        _capture_origin = origin
        _capture_size = img.size
        _capture_hwnd = fg
        _capture_title = win32gui.GetWindowText(fg).strip()
        _last_user_hwnd = fg
        return img

    # If a VANTA overlay temporarily owns focus, reuse only the last window
    # that was actually foreground. Never walk Z-order: that is how unrelated
    # Chrome tabs were accidentally treated as the active screen.
    result = _try_capture(_last_user_hwnd)
    if result:
        img, origin = result
        _capture_origin = origin
        _capture_size = img.size
        _capture_hwnd = _last_user_hwnd
        _capture_title = win32gui.GetWindowText(_last_user_hwnd).strip()
        return img

    # On the first request after startup there may be no remembered user HWND,
    # while VANTA's temporary listening overlay owns foreground focus. Walk
    # only a few immediate Z-order siblings and take the first real application;
    # never enumerate or merge content from multiple windows.
    if _is_winter_hwnd(fg) and not _last_user_hwnd:
        candidate = fg
        for _ in range(8):
            candidate = win32gui.GetWindow(candidate, win32con.GW_HWNDNEXT)
            if not candidate:
                break
            result = _try_capture(candidate)
            if result:
                img, origin = result
                _capture_origin = origin
                _capture_size = img.size
                _capture_hwnd = candidate
                _capture_title = win32gui.GetWindowText(candidate).strip()
                _last_user_hwnd = candidate
                return img
    print("[Vision] No verified active app window found. Falling back to desktop without a UI map.")
    return _grab_full_desktop()


# ------------------------------------------------------------------ #
# SELF-PROTECTION — never let the agent click/type into its own
# source code, no matter which editor it's open in.
# ------------------------------------------------------------------ #

_SELF_SOURCE_FILENAMES = ["actions.py", "vision.py", "agent.py", "ui.py"]
_SELF_DIR_NAME = os.path.basename(os.path.dirname(os.path.abspath(__file__))).lower()

# Protect VANTA's own files, not every editor.  A user may explicitly ask
# VANTA to work in Cursor/Antigravity on a different project.
_PROTECTED_TITLE_SUBSTRINGS = [f.lower() for f in _SELF_SOURCE_FILENAMES] + [
    _SELF_DIR_NAME,
]

_WINTER_UI_TITLES = {"VANTA", ""}


def is_protected_window(title: str) -> bool:
    if not title:
        return title in _WINTER_UI_TITLES
    if title in _WINTER_UI_TITLES:
        return True
    tl = title.lower()
    return any(sub in tl for sub in _PROTECTED_TITLE_SUBSTRINGS)


_is_winter_window = is_protected_window


# ------------------------------------------------------------------ #
# OmniParser — icon-shaped element detection
#
# NOTE: the YOLO icon_detect model only finds icon-LOOKING regions.
# It will NEVER find a plain-text button/link ("Sign in", "Subscribe",
# "Add to cart") because there is no icon graphic there to detect —
# that's the actual gap that made website buttons unreliable. See
# _collect_ocr_elements() below, which is the second half of the pair
# (icon detector + OCR) that a full OmniParser pipeline normally uses.
# ------------------------------------------------------------------ #

ENABLE_OMNIPARSER   = os.getenv("WINTER_ENABLE_OMNIPARSER", "0").strip().lower() in {
    "1", "true", "yes", "on"
}
OMNIPARSER_WEIGHTS  = os.path.join(os.path.dirname(os.path.abspath(__file__)), "weights", "icon_detect", "model.pt")
OMNIPARSER_CONF     = 0.32
OMNIPARSER_DEVICE   = "cuda:0"

_omniparser_model = None
_omniparser_load_failed = False
_resolved_device = "cpu"

def _load_omniparser_model():
    global _omniparser_model, _omniparser_load_failed, _resolved_device
    if _omniparser_model is not None or _omniparser_load_failed:
        return _omniparser_model
    if not ENABLE_OMNIPARSER:
        print("[OmniParser] Disabled for main-process stability; using Windows UI Automation.")
        _omniparser_load_failed = True
        return None
    try:
        from ultralytics import YOLO
        import torch
        import warnings
        warnings.filterwarnings("ignore", category=UserWarning, module="torch.nn.modules.module")
        torch.backends.cudnn.enabled = False
        _resolved_device = OMNIPARSER_DEVICE if torch.cuda.is_available() else "cpu"
        _omniparser_model = YOLO(OMNIPARSER_WEIGHTS)
        _omniparser_model.to(_resolved_device)
        print(f"[OmniParser] Loaded icon_detect on {_resolved_device}")
    except Exception as e:
        print(f"[OmniParser] Could not load ({e}) — continuing with UIA + OCR only.")
        _omniparser_load_failed = True
        _omniparser_model = None
    return _omniparser_model

def _collect_omniparser_elements(screenshot):
    """
    Icon-shaped regions only. Text buttons are NOT covered here —
    see _collect_ocr_elements for that.
    """
    model = _load_omniparser_model()
    if model is None:
        return []

    rects = []
    try:
        with _vision_lock:
            results = model.predict(screenshot, conf=OMNIPARSER_CONF, verbose=False, device=_resolved_device)
        if results:
            for box in results[0].boxes.xyxy.tolist():
                x1, y1, x2, y2 = (int(v) for v in box)
                if x2 - x1 > 4 and y2 - y1 > 4:
                    rects.append((x1, y1, x2, y2, True))
    except Exception as e:
        print(f"[OmniParser] Inference failed: {e}")
    # Free reserved-but-unallocated VRAM so EasyOCR (CPU) and other calls don't OOM
    try:
        import torch
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
    except Exception:
        pass
    print(f"[OmniParser] Detected {len(rects)} icon regions")
    return rects


# ------------------------------------------------------------------ #
# OCR-based text-button/link detection
#
# This is the actual fix for "everything works except clicking on
# websites". Most real website buttons and links are just styled TEXT
# with no distinct icon graphic — OmniParser's icon detector will
# never find them, and UIA's accessibility tree in Chromium is often
# incomplete or slow to populate (see warm_up_browser_windows below).
# This adds a second, independent detector pass that reads any visible
# text on screen and turns each text block into its own clickable
# candidate with an ID box that shows the EXACT text under it — so the
# model can pick "Sign in [14]" directly instead of guessing pixel
# coordinates near text it can only see in the screenshot.
# ------------------------------------------------------------------ #

_ocr_reader = None
_ocr_load_failed = False
_ocr_disabled_logged = False

def _load_ocr_reader():
    global _ocr_reader, _ocr_load_failed, _ocr_disabled_logged
    if _ocr_reader is not None or _ocr_load_failed:
        return _ocr_reader
    # EasyOCR pulls native Torch/OpenCV code into VANTA's main process.  On
    # some Windows machines that stack can terminate the process outright
    # (with no catchable Python exception).  UIA and OmniParser remain the
    # default detectors; explicitly opt in when OCR-only labels are needed.
    if os.getenv("WINTER_ENABLE_EASYOCR", "0").strip().lower() not in {
        "1", "true", "yes", "on"
    }:
        if not _ocr_disabled_logged:
            print("[OCR] EasyOCR disabled for process stability; set WINTER_ENABLE_EASYOCR=1 to opt in.")
            _ocr_disabled_logged = True
        return None
    try:
        import easyocr
        # Force CPU — the 4GB GPU is already consumed by OmniParser's YOLO model.
        # EasyOCR on CPU is fast enough (~0.5s per frame) and prevents CUDA OOM.
        _ocr_reader = easyocr.Reader(['en'], gpu=False)
        print(f"[OCR] easyocr reader loaded (gpu=False, CPU mode to avoid VRAM conflict).")
    except Exception as e:
        print(f"[OCR] Could not load easyocr ({e}) — text buttons/links on "
              f"webpages will not be individually clickable. Run: "
              f"pip install easyocr --break-system-packages")
        _ocr_load_failed = True
        _ocr_reader = None
    return _ocr_reader

def _collect_ocr_elements(screenshot, max_regions=90):
    """
    Returns rects in the same (x1,y1,x2,y2,is_priority,label) shape as
    UIA/OmniParser rects, one per readable text block. Confidence is
    filtered to cut down on OCR noise (stray single characters, UI
    chrome artifacts) that would otherwise crowd the ID-box budget.
    """
    reader = _load_ocr_reader()
    if reader is None:
        return []

    import numpy as np
    arr = np.array(screenshot)
    rects = []
    try:
        with _vision_lock:
            results = reader.readtext(arr, detail=1, paragraph=False)
        for bbox, text, conf in results:
            text = text.strip()
            if not text or conf < 0.45 or len(text) > 40:
                continue
            xs = [p[0] for p in bbox]
            ys = [p[1] for p in bbox]
            x1, y1, x2, y2 = int(min(xs)), int(min(ys)), int(max(xs)), int(max(ys))
            # Pad a little: OCR boxes hug glyphs tightly, but the real
            # clickable hit-region (the button) usually extends a few
            # px around the text on most sites, so a dead-center click
            # on a tight text-only box can still land just outside the
            # actual button/link.
            pad_x, pad_y = 5, 4
            x1, y1 = max(0, x1 - pad_x), max(0, y1 - pad_y)
            x2, y2 = x2 + pad_x, y2 + pad_y
            if (x2 - x1) > 3 and (y2 - y1) > 3:
                rects.append((x1, y1, x2, y2, True, text))
    except Exception as e:
        print(f"[OCR] Inference failed: {e}")

    # Keep the shortest/most button-like text first if we're over
    # budget — long paragraphs are rarely the click target and just
    # eat into the ID slots real buttons need.
    if len(rects) > max_regions:
        rects = sorted(rects, key=lambda r: len(r[5]))[:max_regions]

    print(f"[OCR] Detected {len(rects)} text regions")
    return rects


# ------------------------------------------------------------------ #
# Browser accessibility warm-up
#
# Chromium (Chrome/Edge) only fully populates its UIA tree once it
# detects an assistive-tech client, and that population is NOT
# instant. A screenshot taken immediately after opening/navigating a
# browser window can walk a nearly-empty tree — the address bar, tabs,
# and page content simply have no ID boxes yet, which is what made the
# model click blind coordinates or the wrong element on websites. This
# pokes the window's UIA tree repeatedly and waits for the child count
# to stop growing (i.e. the tree actually finished populating) instead
# of guessing a fixed sleep.
# ------------------------------------------------------------------ #

_BROWSER_PROCESS_NAMES = {"chrome.exe", "msedge.exe", "firefox.exe", "brave.exe"}

def warm_up_browser_windows(max_wait=2.5, poll_interval=0.2):
    import win32process
    import psutil

    def _enum(hwnd, acc):
        if not win32gui.IsWindowVisible(hwnd):
            return True
        try:
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            name = psutil.Process(pid).name().lower()
            if name in _BROWSER_PROCESS_NAMES:
                acc.append(hwnd)
        except Exception:
            pass
        return True

    hwnds = []
    try:
        win32gui.EnumWindows(_enum, hwnds)
    except Exception:
        return

    if not hwnds:
        return

    waited = 0.0
    last_counts = {h: -1 for h in hwnds}
    while waited < max_wait:
        stable = True
        for hwnd in hwnds:
            try:
                root = auto.ControlFromHandle(hwnd)
                children = list(root.GetChildren())
                count = len(children)
                # Nudge one level deeper so the renderer's accessibility
                # tree actually starts being built server-side, instead
                # of just reading whatever snapshot already existed.
                try:
                    for c in children:
                        _ = c.GetChildren()
                except Exception:
                    pass
                if count != last_counts[hwnd]:
                    stable = False
                last_counts[hwnd] = count
            except Exception:
                pass
        if stable and waited > 0.3:
            break
        time.sleep(poll_interval)
        waited += poll_interval

    print(f"[Vision] Browser UIA warm-up done after {waited:.1f}s "
          f"(counts: {list(last_counts.values())})")


# ------------------------------------------------------------------ #
# UI Automation — real element detection
# ------------------------------------------------------------------ #

_INTERESTING_TYPES = {
    auto.ControlType.ButtonControl,
    auto.ControlType.EditControl,
    auto.ControlType.CheckBoxControl,
    auto.ControlType.RadioButtonControl,
    auto.ControlType.ComboBoxControl,
    auto.ControlType.ListItemControl,
    auto.ControlType.MenuItemControl,
    auto.ControlType.TabItemControl,
    auto.ControlType.TreeItemControl,
    auto.ControlType.HyperlinkControl,
    auto.ControlType.DocumentControl,
    auto.ControlType.DataItemControl,
    auto.ControlType.ToolBarControl,
    auto.ControlType.ImageControl,
    auto.ControlType.CustomControl,
    auto.ControlType.MenuControl,
    auto.ControlType.MenuBarControl,
    auto.ControlType.SplitButtonControl,
}

_PRIORITY_TYPES = {
    auto.ControlType.ButtonControl,
    auto.ControlType.EditControl,
    auto.ControlType.CheckBoxControl,
    auto.ControlType.RadioButtonControl,
    auto.ControlType.ComboBoxControl,
    auto.ControlType.ListItemControl,
    auto.ControlType.MenuItemControl,
    auto.ControlType.TabItemControl,
    auto.ControlType.TreeItemControl,
    auto.ControlType.HyperlinkControl,
    auto.ControlType.DataItemControl,
    auto.ControlType.MenuBarControl,
    auto.ControlType.SplitButtonControl,
}

element_map = {}
_MAX_UIA_ELEMENTS = 350
_MIN_AREA_RATIO_TO_WIN = 0.3

_ZOOM_RADIUS     = 180
_ZOOM_SCALE      = 4
_ZOOM_TICK_STEP  = 20
_ZOOM_LABEL_STEP = 100


def take_zoom_crop_b64(base_dir, center_x, center_y, radius=_ZOOM_RADIUS, scale=_ZOOM_SCALE):
    """
    Grab a FRESH full-virtual-desktop screenshot, crop around
    (center_x, center_y) — IMAGE-RELATIVE coords — upscale, and draw a
    ruler in the SAME image-relative coordinate system.

    IMPORTANT: unlike the ID-box overlay, ZOOM shows the RAW
    screenshot — it does not hide protected windows. actions.py's
    is_protected_window() check at click-time is what actually stops
    the model from clicking into its own source through this path;
    don't rely on ZOOM's output alone to keep the agent out of
    dangerous windows.
    """
    screenshot = _grab_active_window() if CAPTURE_ACTIVE_WINDOW_ONLY else _grab_full_desktop()
    img_w, img_h = screenshot.size

    center_x = max(0, min(int(center_x), img_w - 1))
    center_y = max(0, min(int(center_y), img_h - 1))

    x1 = max(0, center_x - radius)
    y1 = max(0, center_y - radius)
    x2 = min(img_w, center_x + radius)
    y2 = min(img_h, center_y + radius)

    crop = screenshot.crop((x1, y1, x2, y2))

    _, sensitive = _collect_ui_elements()
    draw_crop = ImageDraw.Draw(crop)
    for sx1, sy1, sx2, sy2 in sensitive:
        rel_x1 = max(0, sx1 - x1)
        rel_y1 = max(0, sy1 - y1)
        rel_x2 = min(crop.width, sx2 - x1)
        rel_y2 = min(crop.height, sy2 - y1)
        if rel_x2 > rel_x1 and rel_y2 > rel_y1:
            draw_crop.rectangle([rel_x1, rel_y1, rel_x2, rel_y2], fill=(0, 0, 0))

    zoom_w, zoom_h = max(1, crop.width * scale), max(1, crop.height * scale)
    zoomed = crop.resize((zoom_w, zoom_h), Image.LANCZOS)
    draw = ImageDraw.Draw(zoomed)

    try:
        font = ImageFont.truetype("arial.ttf", 13)
    except Exception:
        font = ImageFont.load_default()

    TICK_COL = (60, 220, 120)
    LABEL_BG = (0, 40, 10)
    LABEL_FG = (120, 255, 170)

    first_x = (x1 // _ZOOM_TICK_STEP) * _ZOOM_TICK_STEP
    xv = first_x
    while xv <= x2:
        if xv >= x1:
            zx = int((xv - x1) * scale)
            draw.line([(zx, 0), (zx, zoom_h)], fill=TICK_COL, width=1)
            if xv % _ZOOM_LABEL_STEP == 0:
                label = str(xv)
                lw = 6 + len(label) * 7
                draw.rectangle([zx + 1, 1, zx + 1 + lw, 15], fill=LABEL_BG)
                draw.text((zx + 3, 1), label, fill=LABEL_FG, font=font)
        xv += _ZOOM_TICK_STEP

    first_y = (y1 // _ZOOM_TICK_STEP) * _ZOOM_TICK_STEP
    yv = first_y
    while yv <= y2:
        if yv >= y1:
            zy = int((yv - y1) * scale)
            draw.line([(0, zy), (zoom_w, zy)], fill=TICK_COL, width=1)
            if yv % _ZOOM_LABEL_STEP == 0:
                label = str(yv)
                lw = 6 + len(label) * 7
                draw.rectangle([1, zy + 1, 1 + lw, zy + 15], fill=LABEL_BG)
                draw.text((3, zy + 1), label, fill=LABEL_FG, font=font)
        yv += _ZOOM_TICK_STEP

    gx = int((center_x - x1) * scale)
    gy = int((center_y - y1) * scale)
    draw.line([(gx - 12, gy), (gx + 12, gy)], fill=(255, 60, 60), width=2)
    draw.line([(gx, gy - 12), (gx, gy + 12)], fill=(255, 60, 60), width=2)

    debug_dir = os.path.join(base_dir, "debug")
    os.makedirs(debug_dir, exist_ok=True)
    try:
        zoomed.convert("RGB").save(os.path.join(debug_dir, "last_zoom.jpg"), "JPEG", quality=92)
    except Exception as e:
        print(f"[Vision] Could not save zoom debug screenshot: {e}")

    buf = BytesIO()
    zoomed.convert("RGB").save(buf, "JPEG", quality=92)
    return base64.b64encode(buf.getvalue()).decode(), (x1, y1, x2, y2)


def _rect_area(r):
    x1, y1, x2, y2 = r[0], r[1], r[2], r[3]
    return max(0, x2 - x1) * max(0, y2 - y1)

def _overlap_ratio(a, b):
    ax1, ay1, ax2, ay2 = a[0], a[1], a[2], a[3]
    bx1, by1, bx2, by2 = b[0], b[1], b[2], b[3]
    ix1, iy1 = max(ax1, bx1), max(ay1, by1)
    ix2, iy2 = min(ax2, bx2), min(ay2, by2)
    if ix2 <= ix1 or iy2 <= iy1:
        return 0.0
    inter = (ix2 - ix1) * (iy2 - iy1)
    smaller = min(_rect_area(a), _rect_area(b))
    if smaller <= 0:
        return 0.0
    return inter / smaller

def _collect_ui_elements(max_depth=30):
    """
    Returns (rects, sensitive_rects) in IMAGE-RELATIVE pixels.
    Each rect is (x1, y1, x2, y2, is_priority, label) where label is
    prefixed with the OWNING WINDOW's title, e.g. "Notepad: Text
    Editor [12]" vs "actions.py - VS Code: Document [7]" — so the
    model can visually tell two same-typed controls in different apps
    apart. Windows matched by is_protected_window() are skipped
    entirely.
    """
    rects = []
    sensitive_rects = []
    ox, oy = get_capture_origin()
    img_w, img_h = get_capture_size()

    # The UI map must describe the same window as the screenshot.  Enumerating
    # every visible top-level window leaked unrelated tabs/files into the model
    # context and caused confident but false descriptions.
    target_hwnds = []
    if _capture_hwnd:
        try:
            title = win32gui.GetWindowText(_capture_hwnd)
            if title and not is_protected_window(title):
                target_hwnds.append((_capture_hwnd, title))
        except Exception as e:
            print(f"[UIA] Captured-window lookup failed: {e}")

    occluding_rects = []

    def walk(ctrl, depth, win_title_short):
        if ctrl is None or depth > max_depth:
            return
        try:
            ct = ctrl.ControlType

            is_password = False
            try:
                is_password = bool(ctrl.GetPattern(auto.PatternId.ValuePattern) and ctrl.IsPassword)
            except Exception:
                try:
                    is_password = bool(ctrl.IsPassword)
                except Exception:
                    pass

            elem_name = ""
            try:
                elem_name = ctrl.Name or ""
            except Exception:
                pass

            if (ct in _INTERESTING_TYPES or is_password) and ctrl.IsEnabled and not ctrl.IsOffscreen:
                r = ctrl.BoundingRectangle
                if r and r.width() > 4 and r.height() > 4:
                    x1 = int(r.left) - ox
                    y1 = int(r.top) - oy
                    x2 = int(r.right) - ox
                    y2 = int(r.bottom) - oy
                    x1 = max(0, x1)
                    y1 = max(0, y1)
                    x2 = min(img_w, x2)
                    y2 = min(img_h, y2)
                    if x2 - x1 > 4 and y2 - y1 > 4:
                        cx, cy = (x1 + x2) // 2, (y1 + y2) // 2
                        is_occluded = False
                        for oxr1, oyr1, oxr2, oyr2 in occluding_rects:
                            if oxr1 <= cx <= oxr2 and oyr1 <= cy <= oyr2:
                                is_occluded = True
                                break

                        if not is_occluded:
                            if is_password:
                                sensitive_rects.append((x1, y1, x2, y2))
                            if ct in _INTERESTING_TYPES:
                                is_priority = ct in _PRIORITY_TYPES
                                if elem_name:
                                    label = f"{win_title_short}: {elem_name}" if win_title_short else elem_name
                                else:
                                    label = win_title_short
                                rects.append((x1, y1, x2, y2, is_priority, label))
        except Exception:
            pass
        try:
            for child in ctrl.GetChildren():
                walk(child, depth + 1, win_title_short)
        except Exception:
            pass

    for hwnd, title in target_hwnds:
        try:
            root = auto.ControlFromHandle(hwnd)
            before = len(rects)
            win_title_short = title[:18] + "…" if len(title) > 18 else title
            walk(root, 0, win_title_short)

            win_r = win32gui.GetWindowRect(hwnd)
            occluding_rects.append((win_r[0] - ox, win_r[1] - oy, win_r[2] - ox, win_r[3] - oy))

            print(f"[UIA] '{title}': {len(rects) - before} elements")
        except Exception:
            pass

    print(f"[UIA] Total elements found (pre-filter): {len(rects)}")
    return rects, sensitive_rects

def _dedupe_and_cap(rects):
    """
    Two things changed here vs. the original:

    1. Oversized "container" elements are dropped BEFORE the overlap
       pass. A DocumentControl/CustomControl that covers most of a
       webpage sometimes survives as a single huge box; clicking its
       geometric center almost never hits an actual button — it's
       just whitespace — and worse, its size lets it win the overlap
       check below and swallow the real, smaller controls underneath
       it.
    2. Non-priority duplicates are compared against priority ones
       exactly as before, but priority now includes OCR text rects
       (see _collect_ocr_elements), so a UIA button and an OCR text
       box for the SAME visible button collapse into one ID instead
       of drawing two overlapping boxes for the same click target.
    """
    img_w, img_h = get_capture_size()
    screen_area = img_w * img_h

    rects = [r for r in rects if _rect_area(r) < 0.35 * screen_area]

    rects = sorted(rects, key=_rect_area)
    kept = []

    for r in rects:
        if len(kept) >= _MAX_UIA_ELEMENTS:
            break

        is_duplicate = False
        for k in kept:
            if _overlap_ratio(r, k) <= 0.6:
                continue
            k_is_priority = k[4] if len(k) > 4 else False
            size_ratio = _rect_area(k) / max(1, _rect_area(r))
            if k_is_priority or size_ratio >= _MIN_AREA_RATIO_TO_WIN:
                is_duplicate = True
                break
        if is_duplicate:
            continue

        kept.append(r)

    MIN_CENTER_DIST = 24  # pixels, in native (pre-downscale) resolution

    def _center(r):
        return ((r[0] + r[2]) / 2, (r[1] + r[3]) / 2)

    spaced = []
    for r in kept:
        cx, cy = _center(r)
        too_close = False
        for i, s in enumerate(spaced):
            sx, sy = _center(s)
            if ((cx - sx) ** 2 + (cy - sy) ** 2) ** 0.5 < MIN_CENTER_DIST:
                # keep whichever is priority; if both/neither, keep the first (larger area, since kept is area-sorted)
                r_priority = r[4] if len(r) > 4 else False
                s_priority = s[4] if len(s) > 4 else False
                if r_priority and not s_priority:
                    spaced[i] = r
                too_close = True
                break
        if not too_close:
            spaced.append(r)

    print(f"[UIA] Kept after center-spacing pass: {len(spaced)}")
    return spaced

def take_screenshot_b64(base_dir):
    global element_map
    element_map.clear()

    screenshot = _grab_active_window() if CAPTURE_ACTIVE_WINDOW_ONLY else _grab_full_desktop()

    debug_dir = os.path.join(base_dir, "captures")
    os.makedirs(debug_dir, exist_ok=True)
    debug_path = os.path.join(debug_dir, "last_annotated.jpg")
    try:
        screenshot.save(debug_path, "JPEG", quality=90)
    except Exception as e:
        print(f"[Vision] Could not save debug screenshot: {e}")

    global _send_scale
    screenshot, _send_scale = _downscale_for_model(screenshot)

    buf = BytesIO()
    screenshot.save(buf, "JPEG", quality=90)
    return base64.b64encode(buf.getvalue()).decode(), ""

def screen_regions_from_b64(image_b64):
    """Derive readable overlapping regions from an already-authorized capture."""
    if not image_b64:
        return []
    try:
        image = Image.open(BytesIO(base64.b64decode(image_b64))).convert("RGB")
        width, height = image.size
        if width < 900 or height < 400:
            return []
        regions = [
            ("left side", image.crop((0, 0, int(width * 0.58), height))),
            ("right side", image.crop((int(width * 0.42), 0, width, height))),
        ]
        encoded = []
        for label, crop in regions:
            # Upscale a little so small article text and chart labels survive.
            if crop.width < 1100:
                scale = min(1.7, 1100 / crop.width)
                crop = crop.resize((int(crop.width * scale), int(crop.height * scale)), Image.LANCZOS)
            buf = BytesIO()
            crop.save(buf, "JPEG", quality=92)
            encoded.append((label, base64.b64encode(buf.getvalue()).decode()))
        return encoded
    except Exception as exc:
        print(f"[Vision] Could not derive screen regions: {exc}")
        return []

def take_webcam_b64(base_dir):
    """
    Captures a frame from the webcam (preferring external if available, then fallback to internal).
    Returns the base64 encoded JPEG string.
    """
    # Try index 0 first (default/integrated front camera)
    cap = cv2.VideoCapture(0)
    if not cap.isOpened() or not cap.read()[0]:
        cap.release()
        cap = cv2.VideoCapture(1)
        
    if not cap.isOpened():
        print("[Vision] Error: No webcam detected.")
        return None

    # Let the camera warm up and flush the buffer to avoid stale/dark frames
    time.sleep(0.5)
    for _ in range(5):
        cap.read()
    
    ret, frame = cap.read()
    cap.release()

    if not ret or frame is None:
        print("[Vision] Error: Could not read frame from webcam.")
        return None

    # Convert BGR to RGB (OpenCV uses BGR by default)
    frame_rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
    pil_img = Image.fromarray(frame_rgb)
    
    debug_dir = os.path.join(base_dir, "captures")
    os.makedirs(debug_dir, exist_ok=True)
    debug_path = os.path.join(debug_dir, "last_webcam.jpg")
    try:
        pil_img.save(debug_path, "JPEG", quality=90)
    except Exception as e:
        print(f"[Vision] Could not save webcam debug image: {e}")

    buf = BytesIO()
    pil_img.save(buf, "JPEG", quality=90)
    return base64.b64encode(buf.getvalue()).decode()
