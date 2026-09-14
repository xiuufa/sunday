"""
BaseModule: 模块基类, 提供统一初始化与工具方法规范
另外提供模块间共享的小工具(展示名解析 / 打开浏览器), 避免各模块各写一份

约定:
    - call(): 启动初始化, 程序启动时由 sun_cli.py 自动调用一次
    - tool(): GUI 右键工具入口, 在桌宠右键工具面板显示为按钮
    - name: 模块名(字符串或列表, 列表时第一个为展示名)
    - call_is_runnig / tool_is_runnig: 防重复调用标志
"""

import subprocess
import sys


def display_name(inst, fallback: str = "?") -> str:
    """模块展示名: name 为列表时取第一个, 否则取 name 本身"""
    names = getattr(inst, "name", None)
    if isinstance(names, list):
        return names[0] if names else fallback
    return names or fallback


def open_url(url: str, g=None):
    """用系统默认浏览器打开链接; 进程独立于终端, 不占用终端也不回吐输出

    失败时不抛出, 有 GUI 则弹气泡提示
    """
    try:
        if sys.platform.startswith("win"):
            subprocess.Popen(["cmd", "/c", "start", "", url],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        elif sys.platform == "darwin":
            subprocess.Popen(["open", url],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             start_new_session=True)
        else:
            subprocess.Popen(["xdg-open", url],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             start_new_session=True)
    except Exception as e:
        mgmg = getattr(g, "mgmg", None)
        if mgmg is not None:
            mgmg.dm_print(f"打开失败: {e}", 3000)


class BaseModule:
    def __init__(self, g):
        self.g = g
        self.name: str = "base"
        # call / tool 防重入标志
        self.call_is_runnig: bool = False
        self.tool_is_runnig: bool = False

    def call(self):
        """启动初始化, 程序启动时自动调用一次"""
        if self.call_is_runnig:
            return
        self.call_is_runnig = True

    def tool(self, name: str = ""):
        """GUI 右键工具入口, 在桌宠右键面板显示为按钮并被点击触发"""
        pass
