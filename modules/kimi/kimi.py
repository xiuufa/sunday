"""Kimi: 用默认浏览器打开 kimi.com"""

import subprocess
import sys

URL = "https://kimi.com"


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
    role = "kimi"
    name = "kimi"
    desc = "Kimi: 用默认浏览器打开 kimi.com"
    subcommands = ["help"]

    def run(self):
        """sun kimi: 打开 Kimi"""
        _open(URL)

    def help(self):
        """sun kimi help: 帮助"""
        print(self.desc)
        print("用法:")
        print(f"  sun {self.role}        用默认浏览器打开 {URL}")
