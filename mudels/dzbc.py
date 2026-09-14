"""
豆包模块: 用默认浏览器打开 doubao.com

功能:
    - run(): CLI 入口 (sun dzbc)，用默认浏览器打开 doubao.com
    - tool(): GUI 入口，右键工具面板按钮触发
"""

from base import open_url

URL = "https://doubao.com"


class Dzbc:
    def __init__(self, G):
        self.g = G
        self.name = ["豆包(dzbc)", "dzbc"]
        self.run_methods = ["run"]

    def run(self, name: str = ""):
        """CLI 入口: sun dzbc"""
        open_url(URL, self.g)

    def tool(self):
        """GUI 右键工具入口"""
        open_url(URL, self.g)
