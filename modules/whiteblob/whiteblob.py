"""whiteblob: 桌面窗口模块(桌宠) —— core 侧 facade

窗口是独立进程, 直接入口:
    python -m modules.whiteblob.serve

core 侧这个类只做声明与代理, 不 import tkinter; 窗口状态全在服务进程里。
服务日志: core/services/gui.log

程序接口(供其他模块/脚本调用):
    print(text, duration=3000)     气泡显示文字
    create_btn(text, command="")   工具面板添加按钮, 点击执行 sun <command>

CLI:
    sun gui                        启动/关闭窗口进程
    sun gui stop                   关闭窗口进程
    sun gui print <文字>           气泡显示文字
    sun gui create_btn <文字> [命令]
    sun gui status                 进程状态
    sun gui help                   帮助
"""

import json
import socket
import sys
import time

from modules.whiteblob import common

SERVICE_NAME = common.SERVICE_NAME
LOG_PATH = common.APP_PATH / "core" / "services" / f"{SERVICE_NAME}.log"


class Module:
    role = "gui"
    name = "whiteblob"
    desc = "桌面窗口: 气泡文字与工具按钮"
    subcommands = ["run", "stop", "print", "create_btn", "status", "help"]

    # ---------- 生命周期 ----------

    def __init__(self):
        """挂载时登记窗口进程; 是否启动由 init() 决定"""
        G.server.register(
            SERVICE_NAME,
            [sys.executable, "-m", "modules.whiteblob.serve"],
            marker="modules.whiteblob.serve",
        )

    def init(self):
        """core 初始化时直接启动窗口进程"""
        self.run()

    # ---------- CLI ----------

    def run(self):
        """sun gui: 启动(已运行则关闭)窗口进程"""
        if G.server.status(SERVICE_NAME)["running"]:
            self.stop()
            return
        if not G.server.start(SERVICE_NAME):
            print(f"gui 启动失败, 查看 {LOG_PATH}")
            return
        if G.server.wait(SERVICE_NAME, timeout=5, probe=lambda: self._send({"cmd": "ping"})):
            print("gui 已启动")
        else:
            print(f"gui 启动失败, {LOG_PATH} 尾部:")
            print(self._log_tail())

    def stop(self):
        """sun gui stop: 关闭窗口进程"""
        self._send({"cmd": "quit"})
        for _ in range(20):
            if not G.server.status(SERVICE_NAME)["running"]:
                break
            time.sleep(0.1)
        G.server.stop(SERVICE_NAME)
        print("gui 已停止")

    def status(self):
        """sun gui status: 窗口进程状态"""
        st = G.server.status(SERVICE_NAME)
        pid = f" (pid={st['pid']})" if st["pid"] else ""
        print(f"gui: {'运行中' if st['running'] else '未运行'}{pid}")

    def print(self, text="", duration="3000"):
        """sun gui print <文字>: 气泡显示文字"""
        try:
            ms = int(duration)
        except (TypeError, ValueError):
            ms = 3000
        self._notify({"cmd": "print", "text": text, "duration": ms})

    def add_button(self, text="", command="") -> bool:
        """供其他模块调用: 向工具面板添加按钮, 返回是否送达"""
        if not text:
            return False
        return self._send({"cmd": "btn", "text": text, "command": command})

    def create_btn(self, text="", command=""):
        """sun gui create_btn <文字> [sun 命令]: 工具面板添加按钮"""
        if self.add_button(text, command):
            print("已发送")
        else:
            print("gui 未运行")

    def help(self):
        """sun gui help: 帮助"""
        print(self.desc)
        print("用法:")
        print("  sun gui                        启动/关闭窗口进程")
        print("  sun gui stop                   关闭窗口进程")
        print("  sun gui print <文字>           气泡显示文字")
        print("  sun gui create_btn <文字> [命令]")
        print("  sun gui status                 进程状态")
        print("程序接口: gui.print(text, duration=3000) / gui.add_button(text, command) / gui.create_btn(text, command)")
        print("窗口进程: python -m modules.whiteblob.serve (独立程序, 日志 core/services/gui.log)")

    # ---------- 与窗口进程通信 ----------

    def _notify(self, payload: dict):
        if self._send(payload):
            print("已发送")
        else:
            print("gui 未运行")

    @staticmethod
    def _send(payload: dict) -> bool:
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
                sock.settimeout(2)
                sock.connect(str(common.sock_path()))
                sock.sendall(json.dumps(payload, ensure_ascii=False).encode("utf-8") + b"\n")
            return True
        except OSError:
            return False

    @staticmethod
    def _log_tail() -> str:
        try:
            lines = LOG_PATH.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError as e:
            return f"(无法读取日志: {e})"
        return "\n".join(lines[-15:]) or "(日志为空)"
