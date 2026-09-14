"""
剪贴板模块(插件)

功能:
    1. 注册到桌宠右键工具栏, 点击后读取当前剪贴板内容并存为快捷按钮
    2. 快捷按钮交互:
        - 鼠标悬停 → 显示文本内容预览
        - 左键点击 → 将文本写回剪贴板
        - 右键点击 → 删除该按钮
    3. 读写接口: get_text() / set_text(), 供其他模块 IO

说明:
    - 与自动轮询不同, 本模块采用手动触发方式记录剪贴板
    - 点击工具按钮读取当前剪贴板, 避免自动记录干扰正常工作流

兼容性:
    默认使用 tkinter 内置剪贴板(跨平台, Linux/Ubuntu 直接可用, Windows 同样适用)
"""
import tkinter as tk
from tkinter import ttk

# 历史记录条数上限
HISTORY_LIMIT = 5


def _default_root(g=None):
    """优先复用 mgmg 的 Tk 根窗口, 无 GUI 环境时返回 None"""
    try:
        mgmg = getattr(g, "mgmg", None)
        if mgmg is not None and getattr(mgmg, "root", None) is not None:
            return mgmg.root
    except Exception:
        pass
    return None


# ---------------- 剪贴板读写(核心接口, 供其他模块 IO) ----------------

def get_text(root=None, g=None):
    """读取剪贴板文本, 读取失败返回 None"""
    root = root or _default_root(g)
    if root is None:
        return None
    try:
        return root.clipboard_get()
    except Exception:
        return None


def set_text(text, root=None, g=None):
    """写入剪贴板文本, 成功返回 True"""
    root = root or _default_root(g)
    if root is None:
        return False
    try:
        root.clipboard_clear()
        root.clipboard_append(text)
        return True
    except Exception:
        return False


# ---------------- 剪贴板历史条目(快捷按钮) ----------------

class CvClass:
    """一条剪贴板历史: 快捷按钮
    - 鼠标悬停: 显示文本内容预览
    - 左键单击: 把内容写回剪贴板
    - 右键: 删除该条记录
    """

    def __init__(self, clipboard, text):
        self.clipboard = clipboard  # 所属 Clipboard 实例(删除时从历史移除)
        self.g = clipboard.g
        self.mgmg = clipboard.g.mgmg
        self.data = text
        self.preview_win = None  # 悬停预览窗口

        # 按钮文字: 显示"〇"字表示文本类型
        self.btn = ttk.Button(self.mgmg.cv_btn_fam, text='〇', width=2, command=self.on_click)
        self.btn.pack(side=tk.LEFT, padx=2)
        self.btn.bind('<Enter>', self.show_preview)
        self.btn.bind('<Leave>', self.hide_preview)
        self.btn.bind('<Button-3>', lambda e: self.remove())

    def show_preview(self, e=None):
        """鼠标悬停: 在按钮上方显示文本预览"""
        if self.preview_win is None:
            self.preview_win = tk.Toplevel(self.btn)
            self.preview_win.overrideredirect(True)
            self.preview_win.attributes('-topmost', True)
            self.preview_win.attributes('-alpha', 0.9)
            self.preview_win.config(bg='#808080')
            tk.Label(self.preview_win, text=self.data, bg='#808080', fg='#000000',
                     wraplength=250, justify='left', font=('微软雅黑', 10)).pack(padx=6, pady=4)

        self.preview_win.update_idletasks()
        x = self.btn.winfo_rootx()
        y = self.btn.winfo_rooty() - self.preview_win.winfo_reqheight() - 4
        self.preview_win.geometry(f"+{x}+{y}")
        self.preview_win.deiconify()

    def hide_preview(self, e=None):
        """鼠标离开: 隐藏预览窗口"""
        if self.preview_win is not None:
            try:
                self.preview_win.withdraw()
            except Exception:
                pass

    def on_click(self):
        """左键单击: 把该条内容写回剪贴板"""
        if set_text(self.data, g=self.g):
            self.mgmg.dm_print("已复制", 1500)

    def remove(self):
        """右键删除: 销毁按钮, 并刷新重定位气泡"""
        self.destroy()
        self.clipboard.remove(self)

    def destroy(self):
        """销毁按钮与预览窗口"""
        if self.preview_win is not None:
            try:
                self.preview_win.destroy()
            except Exception:
                pass
            self.preview_win = None
        try:
            self.btn.destroy()
        except Exception:
            pass


# ---------------- 剪贴板插件主类 ----------------

class Clipboard:
    """剪贴板插件: 手动记录剪贴板 + 读写接口, 通过 tool() 注册到右键工具栏"""

    def __init__(self, G):
        # g 为全局对象引用, 通过 self.g.xxx 访问其他模块
        self.g = G
        self.name = "clipboard"
        # call_is_runnig 为True时, call方法正在运行中, 不会被重复调用
        self.call_is_runnig = False
        # 历史记录列表(归属本模块实例, 不挂在全局 g 上)
        self.cv_btn_list = []

    def call(self):
        """初始化: 在 mgmg GUI 就绪后创建常驻历史栏"""
        if self.call_is_runnig:
            return

        mgmg = getattr(self.g, "mgmg", None)
        if mgmg is None or getattr(mgmg, "root", None) is None:
            return  # 无 GUI 环境, 仅保留读写接口

        # 常驻历史栏: 挂在气泡窗口顶部
        mgmg.cv_btn_fam = tk.Frame(mgmg.dm_win, bg='#808080')
        mgmg.cv_btn_fam.pack(side=tk.TOP)

        self.call_is_runnig = True
        try:
            self.g.log.run(f"{self.name} 启动成功")
        except Exception:
            pass

    def tool(self):
        """右键工具栏入口: 读取当前剪贴板并创建快捷按钮"""
        # 读取剪贴板内容
        text = get_text(g=self.g)
        if not text:
            return  # 剪贴板为空时不提示，静默退出

        # 添加到历史
        self.add(text)

    # ---------------- 历史操作 ----------------

    def add(self, text):
        """新增一条历史(自动去重, 超出上限丢弃最旧的); 供其他模块(如 lobo)调用"""
        text = (text or "").strip()
        if not text:
            return None
        if any(item.data == text for item in self.cv_btn_list):
            return None
        item = CvClass(self, text)
        self.cv_btn_list.append(item)
        if len(self.cv_btn_list) > HISTORY_LIMIT:
            self.cv_btn_list.pop(0).destroy()
        self.refresh()
        return item

    def remove(self, item):
        """移除一条历史(由 CvClass.remove 调用)并刷新重定位"""
        try:
            self.cv_btn_list.remove(item)
        except ValueError:
            pass
        self.refresh()

    def refresh(self):
        """按历史数量显隐常驻栏, 并重新定位/显隐气泡(增删按钮后调用)"""
        mgmg = getattr(self.g, "mgmg", None)
        if mgmg is None or getattr(mgmg, "root", None) is None:
            return
        fam = getattr(mgmg, "cv_btn_fam", None)
        if fam is None:
            return
        try:
            if self.cv_btn_list:
                if not fam.winfo_manager():
                    fam.pack(side=tk.TOP)
            else:
                fam.pack_forget()  # 无历史时隐藏常驻栏, 避免空气泡
            mgmg.dm_position()
            if mgmg.dm_has_content():
                mgmg.dm_show()
            else:
                mgmg.dm_hide()
        except Exception:
            pass

    # ---------------- 供其他模块 IO 的接口 ----------------

    def get_text(self, root=None):
        """读取剪贴板文本(其他模块可 self.g.clipboard.get_text())"""
        return get_text(root, g=self.g)

    def set_text(self, text, root=None):
        """写入剪贴板文本(其他模块可 self.g.clipboard.set_text())"""
        return set_text(text, root, g=self.g)