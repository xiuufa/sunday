"""lobo 模块内部使用的跨平台剪贴板工具

优先调用系统剪贴板命令（wl-copy/xclip/xsel/pbcopy）；
Linux 桌面没有这些命令时, 用一个常驻 Tk 小进程持有 CLIPBOARD 选区,
保证 core 这种后台进程写入的内容在之后仍然可以粘贴。
"""

import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002


class ClipboardError(RuntimeError):
    pass


def copy_text(text: str):
    """写入剪贴板, 返回 (ok, detail)"""
    try:
        _copy(text)
        return True, ""
    except Exception as exc:
        return False, str(exc)


def paste_text():
    """读取剪贴板, 返回 (ok, text|detail)"""
    try:
        return True, _paste()
    except Exception as exc:
        return False, str(exc)


# ---------------------------------------------------------------- 内部实现

def _run(cmd, data=None, timeout=5):
    return subprocess.run(cmd, input=data, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, timeout=timeout)


def _copy(text: str):
    data = text.encode("utf-8")
    if sys.platform.startswith("win"):
        _win_copy(text)
        return
    if sys.platform == "darwin":
        if not shutil.which("pbcopy"):
            raise ClipboardError("缺少 pbcopy")
        _run(["pbcopy"], data)
        return
    if os.environ.get("WAYLAND_DISPLAY") and shutil.which("wl-copy"):
        _run(["wl-copy"], data)
        return
    if shutil.which("xclip"):
        _run(["xclip", "-selection", "clipboard"], data)
        return
    if shutil.which("xsel"):
        _run(["xsel", "--clipboard", "--input"], data)
        return
    # 兜底: 用一个常驻 Tk 小进程持有剪贴板（core 是后台进程, 直接 set 完就退出不可靠）
    _copy_via_daemon(text)


def _copy_via_daemon(text: str):
    pid_path = _clip_pid_path()
    _kill_clip_daemon()
    env = dict(os.environ)
    proc = subprocess.Popen(
        [sys.executable, str(Path(__file__).resolve()), "--clipd", str(pid_path)],
        stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        env=env, start_new_session=True,
    )
    try:
        proc.stdin.write(text.encode("utf-8"))
        proc.stdin.close()
    except Exception:
        pass
    deadline = time.time() + 3
    while time.time() < deadline:
        if pid_path.exists():
            return
        time.sleep(0.05)
    raise ClipboardError("剪贴板守护进程启动失败")


def _clip_pid_path() -> Path:
    app_path = Path(__file__).resolve().parents[2]
    pid_dir = app_path / "data" / "services"
    try:
        pid_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    return pid_dir / "lobo-clip.pid"


def _kill_clip_daemon():
    pid_path = _clip_pid_path()
    try:
        pid = int(pid_path.read_text(encoding="utf-8").strip())
    except Exception:
        return
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        pass
    for _ in range(20):
        try:
            os.kill(pid, 0)
        except OSError:
            break
        time.sleep(0.05)
    else:
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass
    try:
        pid_path.unlink()
    except OSError:
        pass


def _serve_clipboard(pid_path: Path):
    """守护进程: 持有 CLIPBOARD 选区直到被下一次复制替换"""
    import tkinter

    data = sys.stdin.buffer.read()
    text = data.decode("utf-8", "replace")
    root = tkinter.Tk()
    root.withdraw()
    root.clipboard_clear()
    root.clipboard_append(text)
    root.update()
    pid_path.write_text(str(os.getpid()), encoding="utf-8")
    state = {"stop": False}

    def _on_term(*_):
        state["stop"] = True

    signal.signal(signal.SIGTERM, _on_term)
    signal.signal(signal.SIGINT, _on_term)
    try:
        while not state["stop"]:
            root.update()
            time.sleep(0.1)
    except Exception:
        pass
    finally:
        try:
            root.destroy()
        except Exception:
            pass
        try:
            if pid_path.read_text(encoding="utf-8").strip() == str(os.getpid()):
                pid_path.unlink()
        except Exception:
            pass


def stop_clipboard_daemon():
    _kill_clip_daemon()


def _paste() -> str:
    if sys.platform.startswith("win"):
        return _win_paste()
    if sys.platform == "darwin":
        if not shutil.which("pbpaste"):
            raise ClipboardError("缺少 pbpaste")
        return _run(["pbpaste"]).stdout.decode("utf-8", "replace")
    if os.environ.get("WAYLAND_DISPLAY") and shutil.which("wl-paste"):
        return _run(["wl-paste", "--no-newline"]).stdout.decode("utf-8", "replace")
    if shutil.which("xclip"):
        return _run(["xclip", "-selection", "clipboard", "-o"]).stdout.decode("utf-8", "replace")
    if shutil.which("xsel"):
        return _run(["xsel", "--clipboard", "--output"]).stdout.decode("utf-8", "replace")
    root = _tk()
    try:
        try:
            return root.clipboard_get()
        except Exception:
            return ""
    finally:
        root.destroy()


def _tk():
    try:
        import tkinter
    except Exception as exc:  # pragma: no cover - 取决于本机 python
        raise ClipboardError(f"Tk 不可用: {exc}")
    if not os.environ.get("DISPLAY") and not os.environ.get("WAYLAND_DISPLAY"):
        raise ClipboardError("没有图形会话, 无法访问剪贴板")
    try:
        root = tkinter.Tk()
    except Exception as exc:
        raise ClipboardError(f"无法连接图形会话: {exc}")
    root.withdraw()
    return root


# ---------------------------------------------------------------- Windows

def _win_copy(text: str):
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
    kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
    kernel32.GlobalLock.restype = wintypes.LPVOID
    kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
    user32.SetClipboardData.restype = wintypes.HANDLE
    user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]

    if not user32.OpenClipboard(None):
        raise ClipboardError("无法打开剪贴板")
    try:
        user32.EmptyClipboard()
        data = (text + "\0").encode("utf-16-le")
        handle = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(data))
        if not handle:
            raise ClipboardError("剪贴板内存分配失败")
        ptr = kernel32.GlobalLock(handle)
        ctypes.memmove(ptr, data, len(data))
        kernel32.GlobalUnlock(handle)
        if not user32.SetClipboardData(CF_UNICODETEXT, handle):
            raise ClipboardError("写入剪贴板失败")
    finally:
        user32.CloseClipboard()


def _win_paste() -> str:
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    kernel32.GlobalLock.restype = wintypes.LPVOID
    kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
    user32.GetClipboardData.restype = wintypes.HANDLE
    user32.GetClipboardData.argtypes = [wintypes.UINT]

    if not user32.OpenClipboard(None):
        raise ClipboardError("无法打开剪贴板")
    try:
        handle = user32.GetClipboardData(CF_UNICODETEXT)
        if not handle:
            return ""
        ptr = kernel32.GlobalLock(handle)
        if not ptr:
            return ""
        try:
            return ctypes.wstring_at(ptr)
        finally:
            kernel32.GlobalUnlock(handle)
    finally:
        user32.CloseClipboard()


if __name__ == "__main__" and len(sys.argv) >= 3 and sys.argv[1] == "--clipd":
    _serve_clipboard(Path(sys.argv[2]))
