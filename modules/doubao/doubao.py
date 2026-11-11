"""豆包: 用默认浏览器打开 doubao.com"""

import subprocess
import sys

URL = "https://doubao.com"


def _open(url: str):
    if sys.platform.startswith("win"):
        cmd = ["cmd", "/c", "start", "", url]
    elif sys.platform == "darwin":
        cmd = ["open", url]
    else:
        cmd = ["xdg-open", url]
    subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     start_new_session=not sys.platform.startswith("win"))


class Module:
    role = "dzbc"
    name = "dzbc"
    desc = "豆包: 用默认浏览器打开 doubao.com"
    subcommands = ["help"]

    def init(self):
        """core 初始化时向 GUI 注册快捷按钮; 失败只记录日志"""
        try:
            gui = G.get("gui")
            if gui is None or not hasattr(gui, "add_button"):
                raise RuntimeError("gui 模块不可用")
            if not gui.add_button("豆包", "doubao"):
                raise RuntimeError("gui 未运行")
        except Exception as exc:
            G.log(f"doubao 注册按钮「豆包」失败: {exc}")

    def run(self):
        """sun dzbc: 打开豆包"""
        _open(URL)

    def help(self):
        """sun dzbc help: 帮助"""
        print(self.desc)
        print("用法:")
        print(f"  sun {self.role}        用默认浏览器打开 {URL}")
