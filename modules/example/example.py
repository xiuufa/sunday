"""示例模块

规范:
    modules/example/example.py 是 core 唯一加载目标
    文件里固定导出一个名为 Module 的类
    G / T 由 core 注入到模块全局命名空间, 直接使用, 不要继承

路由:
    sun demo             无子命令 -> run()   (demo 是 role, example 是 name 别名)
    sun demo help        有子命令 -> help()
    sun example ...      通过 name 别名同样调用

元数据:
    同目录 example.json 由 market/开发者维护, core 读取其中的 ID 用于同 role 替换
"""


class Module:
    name = "example"
    role = "demo"
    desc = "示例模块, 演示新规范; 无实际作用"
    # 是否出现在 sun help / 允许 sun <role> 调用
    used = False
    # 可通过 CLI 调用的子命令; run 不在列表里, 它只在无子命令时调用
    subcommands = ["help"]

    def call(self):
        """一次性初始化: 可调整 G.MODULES_LIST 顺序"""
        # 示例: 从初始化列表移除自己; 想让示例模块生效就删掉这一行
        G.MODULES_LIST.remove(self)

    def run(self):
        """sun demo: 无子命令时的默认行为"""
        print("example run")

    def help(self):
        """显示示例模块帮助"""
        print(self.desc)
        print("用法:")
        print(f"  sun {self.role}")
        print(f"  sun {self.role} help")

