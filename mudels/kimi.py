"""
Kimi 模块: 用默认浏览器打开 kimi.com

功能:
    - run(): CLI 入口 (sun kimi)，用默认浏览器打开 kimi.com
    - tool(): GUI 入口，右键工具面板按钮触发
"""

from base import open_url

URL = "https://kimi.com"


class Kimi:
    def __init__(self, G):
        self.g = G
        self.name = ["Kimi", "kimi"]
        self.run_methods = ["run"]

    def run(self, name: str = ""):
        """CLI 入口: sun kimi"""
        open_url(URL, self.g)

    def tool(self):
        """GUI 右键工具入口"""
        open_url(URL, self.g)
