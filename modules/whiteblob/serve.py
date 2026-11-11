#!/usr/bin/env python3
"""gui 窗口进程(独立程序)

由 core 通过 G.server 托管, 也可以脱离 core 直接调试:
    python -m modules.whiteblob.serve

本进程不依赖 core 注入的 G/T; stdout/stderr 由 core 重定向到
core/services/gui.log。窗口状态只活在这个进程里, core 侧 whiteblob 只是代理。
"""

import json
import os
import queue
import shlex
import signal
import socket
import subprocess
import sys
import tkinter as tk
from pathlib import Path
from threading import Thread
from tkinter import ttk

# 兼容 `python modules/whiteblob/serve.py` 这种直接按路径运行的方式
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from modules.whiteblob import common, emo

try:
    from tkinterdnd2 import TkinterDnD, DND_FILES, DND_TEXT
except ImportError:  # 没有 tkinterdnd2 时退化为普通窗口
    TkinterDnD = None
    DND_FILES = DND_TEXT = None

APP_PATH = common.APP_PATH
BG = "#123456"
DRAG_THRESHOLD = 5
CLICK_MS = 300


def _sock_path() -> Path:
    return common.sock_path()


def _alive(widget) -> bool:
    try:
        return widget is not None and widget.winfo_exists()
    except Exception:
        return False


class _GuiApp:
    """窗口进程: 主窗口 + 气泡 + 工具面板 + 表情 + 本地指令 socket"""

    def __init__(self):
        self.root = None
        self.dm = None
        self.text = None
        self.tools_frame = None
        self.buttons = []          # [(按钮文字, sun 命令)]
        self.queue = queue.Queue()
        self.sock = None
        self.stopping = False

        # 表情状态(emo.py 读写)
        self.emo_paths = []
        self.emo_label = None
        self.emo_timer = None
        self.emo_photo = None
        self.emo_current = None
        self.emo_capture = None
        self.emo_playing = False
        self.emo_thread = None
        self.emo_token = 0

        self.pressed = False
        self.press_x = self.press_y = 0
        self.drag_x = self.drag_y = 0
        self.dragging = False
        self.click_count = 0
        self.click_job = None

    # ---------- 启动与清理 ----------

    def serve(self):
        self._build()
        self._listen()
        emo.load(self)
        emo.show(self, 1)
        self.dm_hide()
        try:
            self.root.mainloop()
        finally:
            self._cleanup()

    def _build(self):
        if os.name == "nt":
            try:
                import ctypes
                ctypes.windll.shcore.SetProcessDpiAwareness(1)
            except Exception:
                pass

        self.root = (TkinterDnD.Tk if TkinterDnD else tk.Tk)()
        self.root.overrideredirect(True)
        self.root.attributes("-alpha", 0.6)
        self.root.attributes("-topmost", True)
        self._transparent(self.root)
        self.root.resizable(False, False)
        self.root.geometry("150x150-6-6")

        self.dm = tk.Toplevel(self.root)
        self.dm.overrideredirect(True)
        self.dm.attributes("-alpha", 0.85)
        self.dm.attributes("-topmost", True)
        self._transparent(self.dm)
        self.dm.config(bg=BG)
        self.dm.geometry("-38-180")
        self.dm.withdraw()

        self.root.bind("<Button-1>", self.on_press)
        self.root.bind("<B1-Motion>", self.on_drag)
        self.root.bind("<ButtonRelease-1>", self.on_release)
        self.root.bind("<Button-3>", self.tools)
        if TkinterDnD:
            self.root.drop_target_register(DND_FILES, DND_TEXT)
            self.root.dnd_bind("<<Drop>>", self.on_drop)

    def _cleanup(self):
        for closable in (self.sock,):
            try:
                if closable is not None:
                    closable.close()
            except Exception:
                pass
        try:
            _sock_path().unlink(missing_ok=True)
        except Exception:
            pass

    # ---------- 本地指令 socket ----------

    def _listen(self):
        path = _sock_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.unlink(missing_ok=True)
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.bind(str(path))
        os.chmod(path, 0o600)
        self.sock.listen(8)
        Thread(target=self._accept_loop, daemon=True, name="gui-socket").start()
        self.root.after(50, self._drain)

    def _accept_loop(self):
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, conn):
        with conn:
            try:
                line = conn.makefile("rb").readline(1024 * 1024)
                req = json.loads(line.decode("utf-8"))
            except Exception:
                return
        if isinstance(req, dict):
            self.queue.put(req)

    def _drain(self):
        while True:
            try:
                req = self.queue.get_nowait()
            except queue.Empty:
                break
            self._dispatch(req)
        if not self.stopping:
            self.root.after(50, self._drain)

    def _dispatch(self, req):
        cmd = req.get("cmd")
        if cmd == "print":
            self.bubble(str(req.get("text", "")), int(req.get("duration") or 3000))
        elif cmd == "btn":
            self.add_button(str(req.get("text", "")), str(req.get("command", "")))
        elif cmd == "ping":
            pass  # 健康检查, 无副作用
        elif cmd == "quit":
            self.close()

    # ---------- 程序接口实现 ----------

    def bubble(self, text, duration=3000):
        """气泡显示文字, duration 毫秒后自动关闭"""
        if not _alive(self.dm):
            return
        self.text_close()
        self.text = tk.Label(self.dm, text=text, font=("微软雅黑", 12), wraplength=130,
                             bg="#757575", fg="#000000", padx=10, pady=5)
        self.text.bind("<Double-Button-1>", self.text_close)
        self.text.pack(fill=tk.BOTH, expand=True)
        self.text.after(duration if duration > 0 else 30000, self.text_close)
        self.dm.after(1, lambda: (self.dm_position(), self.dm_show()))

    def add_button(self, text, command=""):
        """工具面板添加按钮; command 是点击后执行的 sun 命令"""
        if not text:
            return
        self.buttons.append((text, command))
        if _alive(self.tools_frame):
            self._render_tools()
        else:
            self.bubble(f"已添加按钮: {text}", 1500)

    def _render_tools(self):
        if _alive(self.tools_frame):
            self.tools_frame.destroy()
        self.tools_frame = tk.Frame(self.dm, bg=BG)
        self.tools_frame.pack(fill=tk.BOTH, expand=True)
        for text, command in self.buttons:
            ttk.Button(self.tools_frame, text=text,
                       command=lambda c=command: self._run_command(c)).pack(fill=tk.X, padx=0, pady=4)
        ttk.Button(self.tools_frame, text="关闭", command=self.close).pack(fill=tk.X)
        self.dm_show()

    def _run_command(self, command):
        if not command:
            return
        launcher = APP_PATH / ("sun.bat" if os.name == "nt" else "sun")
        args = [str(launcher), *shlex.split(command)]
        if os.name == "nt":
            args = ["cmd", "/c", *args]
        try:
            subprocess.Popen(
                args, cwd=str(APP_PATH),
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                start_new_session=os.name == "posix",
            )
        except Exception as e:
            self.bubble(f"执行失败: {e}", 3000)

    # ---------- 气泡窗口 ----------

    def dm_has_content(self):
        return _alive(self.text) or _alive(self.tools_frame)

    def dm_position(self):
        """气泡定位到主窗口正上方居中"""
        if not _alive(self.dm) or not _alive(self.root):
            return

        def place():
            try:
                if not _alive(self.dm) or not _alive(self.root):
                    return
                self.dm.update_idletasks()
                x = self.root.winfo_x() + (self.root.winfo_width() - self.dm.winfo_width()) // 2
                y = self.root.winfo_y() - self.dm.winfo_height() - 6
                self.dm.geometry(f"+{x}+{y}")
            except Exception:
                pass

        self.dm.after(30, place)

    def dm_show(self):
        if not _alive(self.dm) or not self.dm_has_content():
            return
        self.dm.deiconify()
        self.dm_position()

    def dm_hide(self):
        if _alive(self.dm) and not self.dm_has_content():
            self.dm_position()
            self.dm.withdraw()

    def text_close(self, event=None):
        if self.text is not None:
            self.text.destroy()
            self.text = None
            self.dm_hide()
        if _alive(self.dm):
            self.dm_position()

    # ---------- 工具面板 ----------

    def tools(self, event=None):
        if _alive(self.tools_frame):
            self.tools_frame.destroy()
            self.tools_frame = None
            self.dm_position()
            self.dm_hide()
            return
        self.text_close()
        self.dm_position()
        self._render_tools()

    # ---------- 窗口缩放 ----------

    def maximize(self):
        self.root.geometry("150x150")
        self.dm_show()

    def minimize(self):
        if _alive(self.tools_frame):
            self.tools_frame.destroy()
            self.tools_frame = None
            self.dm_position()
            self.dm_hide()
        self.text_close()
        emo.close(self)
        self.root.geometry("30x30-6-60")
        self.dm_show()

    # ---------- 鼠标拖拽与点击 ----------

    def on_press(self, event):
        self.pressed = True
        self.press_x, self.press_y = event.x, event.y
        self.drag_x, self.drag_y = event.x, event.y
        self.dragging = False

    def on_drag(self, event):
        if not self.pressed:
            return
        if abs(event.x - self.press_x) <= DRAG_THRESHOLD and abs(event.y - self.press_y) <= DRAG_THRESHOLD:
            return
        self.dragging = True
        if self.click_job is not None:
            self.root.after_cancel(self.click_job)
            self.click_job = None
            self.click_count = 0
        x = self.root.winfo_x() + (event.x - self.drag_x)
        y = self.root.winfo_y() + (event.y - self.drag_y)
        self.root.geometry(f"+{x}+{y}")
        dm_x = x + (self.root.winfo_width() - self.dm.winfo_width()) // 2
        dm_y = y - self.dm.winfo_height() - 4
        self.dm.geometry(f"+{dm_x}+{dm_y}")

    def on_release(self, event):
        self.pressed = False
        if self.dragging:
            self.dragging = False
            emo.show(self, 1)
            return

        self.click_count += 1
        if self.click_job is not None:
            self.root.after_cancel(self.click_job)

        def finalize():
            count, self.click_count, self.click_job = self.click_count, 0, None
            if count == 1:
                self.maximize()
                emo.show(self, 0)
            elif count == 2:
                self.maximize()
                emo.show(self, 1)
            elif count == 3:
                self.minimize()

        self.click_job = self.root.after(CLICK_MS, finalize)

    def on_drop(self, event):
        """拖入文件/文本: 展开并显示开心表情"""
        self.maximize()
        emo.show(self, 1)

    # ---------- 辅助 ----------

    def emo_root(self) -> Path:
        """表情资源目录: whiteblob.json 的 emo_path, 缺省为模块目录下的 emo/"""
        raw = self._config().get("emo_path")
        if isinstance(raw, str) and raw.strip():
            path = Path(raw.strip())
            return path if path.is_absolute() else APP_PATH / path
        return Path(__file__).resolve().parent / "emo"

    @staticmethod
    def _config() -> dict:
        """模块配置: 同目录 whiteblob.json; core 只读其中的 name/role/desc/ID/used, 其余字段模块自己用"""
        try:
            data = json.loads((Path(__file__).resolve().parent / "whiteblob.json").read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    @staticmethod
    def _transparent(widget):
        """透明色仅 Windows 生效, 其它平台忽略"""
        try:
            widget.wm_attributes("-transparentcolor", BG)
        except tk.TclError:
            pass

    # ---------- 关闭 ----------

    def close(self):
        self.stopping = True
        emo.close(self)
        try:
            if _alive(self.dm):
                self.dm.destroy()
            for child in self.root.winfo_children():
                child.destroy()
            self.root.after(150, self.root.quit)
        except Exception:
            try:
                self.root.quit()
            except Exception:
                pass


def main() -> int:
    print(f"[whiteblob] serve 启动 pid={os.getpid()} socket={_sock_path()}", flush=True)
    app = _GuiApp()

    def _terminate(*_):
        try:
            app.close()
        except Exception:
            pass

    signal.signal(signal.SIGTERM, _terminate)
    app.serve()
    return 0


if __name__ == "__main__":
    sys.exit(main())
