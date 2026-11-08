# -*- coding: utf-8 -*-
"""Windows application identity for every VANTA surface.

Tk windows inherit the executable identity of ``pythonw.exe`` unless the
process claims one of its own.  That is why a perfectly branded desktop
shortcut can still turn into a Python icon once a native result window is
opened.  Configure the process before the first Tk root, then install one
default icon which future toplevels inherit.

All functions are best-effort.  A cosmetic failure must never stop VANTA.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any


APP_USER_MODEL_ID = "VANTA.PersonalAssistant"
_icon_photo = None

# Win32 values are kept here rather than copied into every UI module.  A
# borderless Tk window is *not* automatically a tool window: on Windows 11 it
# can still satisfy the shell's taskbar rules (visible + unowned + not marked
# WS_EX_TOOLWINDOW).  That was the blue VANTA/Python entry users saw even when
# every useful VANTA surface was hidden.
GWL_EXSTYLE = -20
GWLP_HWNDPARENT = -8
GA_ROOT = 2
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_APPWINDOW = 0x00040000
SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_NOZORDER = 0x0004
SWP_NOACTIVATE = 0x0010
SWP_FRAMECHANGED = 0x0020


def hidden_taskbar_exstyle(exstyle: int, *, tool_window: bool = True) -> int:
    """Return an extended style which the Windows shell will not list.

    ``WS_EX_APPWINDOW`` forces an entry even for an owned window, so it must
    always be removed.  Passive overlays also receive ``WS_EX_TOOLWINDOW``.
    The normal framed result reader instead stays a normal-looking window and
    is hidden by native window ownership.
    """
    value = int(exstyle) & ~WS_EX_APPWINDOW
    if tool_window:
        value |= WS_EX_TOOLWINDOW
    else:
        value &= ~WS_EX_TOOLWINDOW
    return value


def _user32():
    if os.name != "nt":
        return None
    try:
        import ctypes
        from ctypes import wintypes
        api = ctypes.windll.user32
        # ctypes otherwise assumes 32-bit integer handles. HWND values happen
        # to be small on many boots, hiding the truncation bug until a long
        # session allocates one above 2 GiB.
        api.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
        api.GetAncestor.restype = wintypes.HWND
        api.GetWindow.argtypes = [wintypes.HWND, wintypes.UINT]
        api.GetWindow.restype = wintypes.HWND
        api.IsWindowVisible.argtypes = [wintypes.HWND]
        api.IsWindowVisible.restype = wintypes.BOOL
        api.SetWindowPos.argtypes = [
            wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
            ctypes.c_int, ctypes.c_int, wintypes.UINT,
        ]
        api.SetWindowPos.restype = wintypes.BOOL
        return api
    except Exception:
        return None


def native_hwnd(window: Any) -> int:
    """Resolve Tk's client handle to the real top-level Win32 wrapper."""
    api = _user32()
    if api is None or window is None:
        return 0
    try:
        window.update_idletasks()
        child = int(window.winfo_id())
        return int(api.GetAncestor(child, GA_ROOT) or child)
    except Exception:
        return 0


def _get_window_long_ptr(api, hwnd: int, index: int) -> int:
    """Pointer-width-safe GetWindowLong wrapper (also works on 32-bit)."""
    import ctypes
    getter = getattr(api, "GetWindowLongPtrW", None) or api.GetWindowLongW
    try:
        getter.argtypes = [ctypes.c_void_p, ctypes.c_int]
        getter.restype = ctypes.c_ssize_t
    except Exception:
        pass
    return int(getter(hwnd, index))


def _set_window_long_ptr(api, hwnd: int, index: int, value: int) -> None:
    """Pointer-width-safe SetWindowLong wrapper."""
    import ctypes
    setter = getattr(api, "SetWindowLongPtrW", None) or api.SetWindowLongW
    try:
        setter.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_ssize_t]
        setter.restype = ctypes.c_ssize_t
    except Exception:
        pass
    setter(hwnd, index, value)


def _apply_taskbar_policy(window: Any, owner: Any = None,
                          *, tool_window: bool = True) -> bool:
    """Apply the native styles once; callers use :func:`hide_from_taskbar`."""
    api = _user32()
    hwnd = native_hwnd(window)
    if api is None or not hwnd:
        return False
    try:
        owner_hwnd = native_hwnd(owner) if owner is not None else 0
        if owner_hwnd and owner_hwnd != hwnd:
            # On Win32 this field is an *owner* for a top-level HWND, not a
            # child-parent relationship. Owned windows stay out of taskbar and
            # minimise with the application without changing Tk geometry.
            _set_window_long_ptr(api, hwnd, GWLP_HWNDPARENT, owner_hwnd)

        current = _get_window_long_ptr(api, hwnd, GWL_EXSTYLE)
        desired = hidden_taskbar_exstyle(current, tool_window=tool_window)
        if desired != current:
            _set_window_long_ptr(api, hwnd, GWL_EXSTYLE, desired)
        api.SetWindowPos(
            hwnd, 0, 0, 0, 0, 0,
            SWP_NOMOVE | SWP_NOSIZE | SWP_NOZORDER |
            SWP_NOACTIVATE | SWP_FRAMECHANGED,
        )
        return True
    except Exception:
        return False


def hide_from_taskbar(window: Any, owner: Any = None,
                      *, tool_window: bool = True) -> bool:
    """Keep a Tk/CTk surface out of the Windows taskbar for its lifetime.

    Tk may remap or recreate a native wrapper when ``overrideredirect`` or
    ``deiconify`` changes.  Applying the flag only at construction therefore
    works until the first sleep/standby/dashboard cycle.  The Map hook
    reapplies the policy after every remap while the direct application avoids
    a first-frame taskbar flash.

    This function is deliberately best-effort; window chrome must never be
    able to stop the assistant.
    """
    if window is None:
        return False
    try:
        window._vanta_taskbar_owner = owner
        window._vanta_taskbar_tool = bool(tool_window)
    except Exception:
        pass

    changed = _apply_taskbar_policy(
        window, owner, tool_window=bool(tool_window))

    try:
        if not getattr(window, "_vanta_taskbar_hooked", False):
            def _on_map(_event=None, target=window):
                def reapply():
                    try:
                        if not target.winfo_exists():
                            return
                    except Exception:
                        return
                    _apply_taskbar_policy(
                        target,
                        getattr(target, "_vanta_taskbar_owner", None),
                        tool_window=bool(getattr(
                            target, "_vanta_taskbar_tool", True)),
                    )
                try:
                    target.after_idle(reapply)
                except Exception:
                    reapply()

            window.bind("<Map>", _on_map, add="+")
            window._vanta_taskbar_hooked = True
    except Exception:
        pass
    return changed


def taskbar_diagnostics(window: Any) -> dict:
    """Return non-mutating shell-relevant state for support/soak tests."""
    api = _user32()
    hwnd = native_hwnd(window)
    if api is None or not hwnd:
        return {"available": False, "hwnd": 0}
    try:
        exstyle = _get_window_long_ptr(api, hwnd, GWL_EXSTYLE)
        owner = int(api.GetWindow(hwnd, 4) or 0)  # GW_OWNER
        visible = bool(api.IsWindowVisible(hwnd))
        return {
            "available": True,
            "hwnd": hwnd,
            "visible": visible,
            "owner": owner,
            "tool_window": bool(exstyle & WS_EX_TOOLWINDOW),
            "app_window": bool(exstyle & WS_EX_APPWINDOW),
            # This mirrors the shell rule relevant to VANTA. The shell has
            # additional special cases, but no VANTA surface uses them.
            "taskbar_candidate": bool(
                visible and not owner and
                not (exstyle & WS_EX_TOOLWINDOW)),
        }
    except Exception:
        return {"available": False, "hwnd": hwnd}


def configure_process() -> bool:
    """Give Windows a stable application identity before Tk creates a HWND."""
    if os.name != "nt":
        return False
    try:
        import ctypes
        result = ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
            APP_USER_MODEL_ID)
        return int(result) == 0
    except Exception:
        return False


def icon_paths(base_dir: str | os.PathLike | None = None) -> tuple[str, str]:
    """Return the idle-orb ICO and PNG, falling back to the legacy mark."""
    root = Path(base_dir or Path(__file__).resolve().parent)
    assets = root / "assets"
    ico = assets / "vanta-idle.ico"
    png = assets / "vanta-idle-64.png"
    if not ico.exists():
        ico = assets / "winter.ico"
    if not png.exists():
        png = assets / "winter-64.png"
    return str(ico), str(png)


def apply_to(root, *windows, base_dir: str | os.PathLike | None = None) -> bool:
    """Apply VANTA's idle icon to Tk's defaults and the supplied windows."""
    global _icon_photo
    ico, png = icon_paths(base_dir)
    targets = tuple(window for window in (root, *windows) if window is not None)
    changed = False

    # ``default=`` is the important part: native Toplevels created later no
    # longer flash or remain as Python in the taskbar.
    if os.path.exists(ico):
        try:
            root.iconbitmap(default=ico)
            changed = True
        except Exception:
            pass
        for window in targets:
            try:
                window.iconbitmap(ico)
                changed = True
            except Exception:
                pass

    if os.path.exists(png):
        try:
            if _icon_photo is None:
                import tkinter as tk
                _icon_photo = tk.PhotoImage(master=root, file=png)
            root.iconphoto(True, _icon_photo)
            for window in targets:
                window.iconphoto(False, _icon_photo)
            changed = True
        except Exception:
            pass
    return changed
