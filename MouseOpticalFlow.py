#!/usr/bin/env python3
"""
PC-side stand-in for MouseControllerIno.ino / ADNS3080_Mouse_Controller_V5,
plus the live visualization from mousecam_motion.py.

Reads relative motion from USB mice plugged into the computer (Windows Raw
Input), assigns the first two mice as bottom/top, prints yaw/pitch/roll at
60 Hz, shifts an ImageWindow from the bottom mouse (like sensor 1 in
mousecam_motion.py), then plots recorded traces when done.

Ctrl+C to stop early (still shows the plot of samples collected so far).
"""

from __future__ import annotations

import sys
import time
from pathlib import Path
from ctypes import (
    POINTER,
    Structure,
    byref,
    cast,
    sizeof,
    windll,
    CFUNCTYPE,
    c_long,
    c_longlong,
    c_ubyte,
    c_ushort,
    c_void_p,
    c_wchar_p,
)
from ctypes.wintypes import DWORD, HWND, LPARAM, MSG, UINT, ULONG, WPARAM

import numpy as np
import matplotlib.pyplot as plt

if sys.platform != "win32":
    print("This script uses the Windows Raw Input API; run it on Windows.", file=sys.stderr)
    sys.exit(1)

# Reuse ImageWindow from the ADNS3080 debugging folder
_DEBUG_DIR = (
    Path(__file__).resolve().parent
    / "MouseGoggles"
    / "Other Hardware"
    / "Spherical Treadmill"
    / "ADNS3080 Debugging"
)
sys.path.insert(0, str(_DEBUG_DIR))
import imagewindow  # noqa: E402

user32 = windll.user32
kernel32 = windll.kernel32

# Match Arduino send_rate
SEND_RATE_HZ = 60.0
SEND_PERIOD_S = 1.0 / SEND_RATE_HZ

# Scale factors (same as ADNS3080_Mouse_Controller_V5 / MouseControllerIno)
BACK_SCALE = 1.0  # should be 0.1?
SCALE_RATIO = 1.0
BOTTOM_SCALE = BACK_SCALE * SCALE_RATIO

# mousecam_motion.py equivalents
NUMBER_OF_DATAPOINTS = 1000
IMAGE_SIZE = 200
MOTION_VIS_SCALE = 4.0  # offset_x += dx / 4.0

# Win32 constants
WM_DESTROY = 0x0002
WM_INPUT = 0x00FF
WM_QUIT = 0x0012
HWND_MESSAGE = HWND(-3)
RID_INPUT = 0x10000003
RIM_TYPEMOUSE = 0
RIDEV_INPUTSINK = 0x00000100
MOUSE_MOVE_ABSOLUTE = 0x01
WS_OVERLAPPED = 0x00000000
PM_REMOVE = 0x0001

LRESULT = c_longlong if sizeof(c_void_p) == 8 else c_long
WNDPROC = CFUNCTYPE(LRESULT, HWND, UINT, WPARAM, LPARAM)


class RAWINPUTDEVICE(Structure):
    _fields_ = [
        ("usUsagePage", c_ushort),
        ("usUsage", c_ushort),
        ("dwFlags", DWORD),
        ("hwndTarget", HWND),
    ]


class RAWINPUTHEADER(Structure):
    _fields_ = [
        ("dwType", DWORD),
        ("dwSize", DWORD),
        ("hDevice", c_void_p),
        ("wParam", WPARAM),
    ]


class RAWMOUSE(Structure):
    # Matches Windows RAWMOUSE (ulButtons is the usButtonFlags/usButtonData union)
    _fields_ = [
        ("usFlags", c_ushort),
        ("ulButtons", ULONG),
        ("ulRawButtons", ULONG),
        ("lLastX", c_long),
        ("lLastY", c_long),
        ("ulExtraInformation", ULONG),
    ]


class RAWINPUT(Structure):
    _fields_ = [
        ("header", RAWINPUTHEADER),
        ("mouse", RAWMOUSE),
    ]


class WNDCLASSW(Structure):
    _fields_ = [
        ("style", UINT),
        ("lpfnWndProc", WNDPROC),
        ("cbClsExtra", c_long),
        ("cbWndExtra", c_long),
        ("hInstance", c_void_p),
        ("hIcon", c_void_p),
        ("hCursor", c_void_p),
        ("hbrBackground", c_void_p),
        ("lpszMenuName", c_wchar_p),
        ("lpszClassName", c_wchar_p),
    ]


# Keep a reference so the callback is not garbage-collected
_WNDPROC_REF = None


@WNDPROC
def _wndproc(hwnd, msg, wparam, lparam):
    if msg == WM_DESTROY:
        user32.PostQuitMessage(0)
        return 0
    return user32.DefWindowProcW(hwnd, msg, wparam, lparam)


class MouseAccum:
    __slots__ = ("dx", "dy", "X", "Y")

    def __init__(self) -> None:
        self.dx = 0
        self.dy = 0
        self.X = 0
        self.Y = 0

    def reset(self) -> None:
        self.X = 0
        self.Y = 0


def create_message_window():
    global _WNDPROC_REF
    _WNDPROC_REF = _wndproc

    hinstance = kernel32.GetModuleHandleW(None)
    class_name = "MouseOpticalFlowRawInput"

    wc = WNDCLASSW()
    wc.style = 0
    wc.lpfnWndProc = _wndproc
    wc.cbClsExtra = 0
    wc.cbWndExtra = 0
    wc.hInstance = hinstance
    wc.hIcon = None
    wc.hCursor = None
    wc.hbrBackground = None
    wc.lpszMenuName = None
    wc.lpszClassName = class_name

    if not user32.RegisterClassW(byref(wc)):
        err = kernel32.GetLastError()
        # 1410 = class already exists
        if err != 1410:
            raise OSError(f"RegisterClassW failed ({err})")

    hwnd = user32.CreateWindowExW(
        0,
        class_name,
        "MouseOpticalFlow",
        WS_OVERLAPPED,
        0,
        0,
        0,
        0,
        HWND_MESSAGE,
        None,
        hinstance,
        None,
    )
    if not hwnd:
        raise OSError(f"CreateWindowExW failed ({kernel32.GetLastError()})")
    return hwnd


def register_raw_input(hwnd) -> None:
    rid = RAWINPUTDEVICE()
    rid.usUsagePage = 0x01  # Generic Desktop
    rid.usUsage = 0x02  # Mouse
    rid.dwFlags = RIDEV_INPUTSINK  # receive while unfocused
    rid.hwndTarget = hwnd
    if not user32.RegisterRawInputDevices(byref(rid), 1, sizeof(RAWINPUTDEVICE)):
        raise OSError(f"RegisterRawInputDevices failed ({kernel32.GetLastError()})")


def read_raw_mouse(lparam) -> tuple[int, int, int] | None:
    """Return (device_handle, dx, dy) or None if not a relative mouse move."""
    size = UINT(0)
    user32.GetRawInputData(
        LPARAM(lparam), RID_INPUT, None, byref(size), sizeof(RAWINPUTHEADER)
    )
    if size.value == 0:
        return None

    buf = (c_ubyte * size.value)()
    copied = user32.GetRawInputData(
        LPARAM(lparam), RID_INPUT, byref(buf), byref(size), sizeof(RAWINPUTHEADER)
    )
    if copied != size.value:
        return None

    raw = cast(buf, POINTER(RAWINPUT)).contents
    if raw.header.dwType != RIM_TYPEMOUSE:
        return None
    if raw.mouse.usFlags & MOUSE_MOVE_ABSOLUTE:
        return None

    handle = int(raw.header.hDevice or 0)
    return handle, int(raw.mouse.lLastX), int(raw.mouse.lLastY)


def pump_messages(device_order: list[int], mice: dict[int, MouseAccum]) -> bool:
    """Process pending WM_INPUT messages. Returns False on WM_QUIT."""
    msg = MSG()
    while user32.PeekMessageW(byref(msg), None, 0, 0, PM_REMOVE):
        if msg.message == WM_QUIT:
            return False
        if msg.message == WM_INPUT:
            parsed = read_raw_mouse(msg.lParam)
            if parsed is None:
                continue
            handle, dx, dy = parsed
            if handle not in mice:
                if len(device_order) >= 2:
                    continue  # ignore mice beyond bottom/top
                mice[handle] = MouseAccum()
                device_order.append(handle)
                role = "bottom" if len(device_order) == 1 else "top"
                print(f"Assigned mouse handle {handle:#x} as {role}", flush=True)
            accum = mice[handle]
            accum.dx = dx
            accum.dy = dy
            accum.X += dx
            accum.Y += dy
        else:
            user32.TranslateMessage(byref(msg))
            user32.DispatchMessageW(byref(msg))
    return True


def make_pattern(size: int = IMAGE_SIZE):
    """Same arbitrary image pattern as mousecam_motion.py."""
    q = np.arange(0, size)
    q = np.expand_dims(q, 1)
    w = np.transpose(q, axes=[1, 0])
    return q, w


def pattern_frame(q, w, offset_x: float, offset_y: float, size: int = IMAGE_SIZE):
    return (np.mod(q + offset_x, size) * np.mod(w + offset_y, size)) / size / size * 255


def plot_traces(dxdys: np.ndarray, n: int) -> None:
    if n <= 2:
        print("Not enough samples to plot.")
        return
    for i in range(4):
        plt.plot(dxdys[2:n, i], label=str(i))
    plt.legend()
    plt.xlabel("sample")
    plt.ylabel("delta")
    plt.title("bottom dx,dy (0,1) and top dx,dy (2,3)")
    plt.show()


def main() -> None:
    hwnd = create_message_window()
    register_raw_input(hwnd)

    device_order: list[int] = []
    mice: dict[int, MouseAccum] = {}
    empty = MouseAccum()
    last_send = time.perf_counter()

    # mousecam_motion-style visualization + recording
    q, w = make_pattern()
    offset_x = 0.0
    offset_y = 0.0
    window = imagewindow.ImageWindow(IMAGE_SIZE, IMAGE_SIZE, name="mousecam motion")
    window.draw(pattern_frame(q, w, offset_x, offset_y))

    dxdys = np.zeros([NUMBER_OF_DATAPOINTS, 6])
    index = 0

    print("Listening for USB mice via Raw Input (Ctrl+C to stop)")
    print("Move each treadmill mouse once so it can be assigned bottom, then top.")
    print(f"Collecting up to {NUMBER_OF_DATAPOINTS} samples, then plotting.")
    print(
        "format: bottom X=.. Y=.. | top X=.. Y=.. | yaw=.. pitch=.. roll=..",
        flush=True,
    )

    try:
        while index < NUMBER_OF_DATAPOINTS:
            if not pump_messages(device_order, mice):
                break

            now = time.perf_counter()
            if (now - last_send) < SEND_PERIOD_S:
                time.sleep(0.001)
                continue
            last_send += SEND_PERIOD_S
            if now - last_send > SEND_PERIOD_S:
                last_send = now

            bottom = mice[device_order[0]] if len(device_order) >= 1 else empty
            top = mice[device_order[1]] if len(device_order) >= 2 else empty

            dx_1, dy_1 = bottom.X, bottom.Y
            dx_2, dy_2 = top.X, top.Y

            # Same formulas as ADNS3080_Mouse_Controller_V5 / MouseControllerIno
            yaw_float = float(dx_1) * BOTTOM_SCALE
            pitch_float = float(dx_2) * BACK_SCALE
            roll_float = 0.5 * (
                float(dy_1) * BOTTOM_SCALE + float(dy_2) * BACK_SCALE
            )

            print(
                f"bottom X={dx_1} Y={dy_1} | "
                f"top X={dx_2} Y={dy_2} | "
                f"yaw={yaw_float} pitch={pitch_float} roll={roll_float}",
                flush=True,
            )

            # Record like mousecam_motion (sq columns unused for USB mice)
            dxdys[index, 0] = dx_1
            dxdys[index, 1] = dy_1
            dxdys[index, 2] = dx_2
            dxdys[index, 3] = dy_2
            dxdys[index, 4] = -1
            dxdys[index, 5] = -1

            # Visualize bottom mouse (sensor 1) by circularly shifting the image
            offset_x += dx_1 / MOTION_VIS_SCALE
            offset_y += dy_1 / MOTION_VIS_SCALE
            # To visualize top mouse instead, use dx_2/dy_2 here
            window.draw(pattern_frame(q, w, offset_x, offset_y))

            index += 1
            bottom.reset()
            top.reset()
    except KeyboardInterrupt:
        print("\nStopped early.")
    finally:
        window.close()
        user32.DestroyWindow(hwnd)

    print(f"Recorded {index} samples.")
    plot_traces(dxdys, index)


if __name__ == "__main__":
    main()
