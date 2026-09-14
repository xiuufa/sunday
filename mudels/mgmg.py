import ctypes
import os
import sys
import tkinter as tk
from pathlib import Path
from tkinter import ttk

from tkinterdnd2 import TkinterDnD, DND_FILES, DND_TEXT

from base import display_name

# GUI 后台服务在 cmd 中的注册名(与模块名一致)
SERVICE_NAME = "mgmg"
# 标识当前进程是 cmd 拉起的 GUI 服务子进程(父进程只负责拉起, 不进入主循环)
SERVICE_ENV = "SUN_DAEMON"


class MgmgGUI:
    # g 是全局对象, 其中挂载着所有模块的实例,用于实现模块之间的通信与调用、重载
    def __init__(self, G):
        # g 为全局对象引用(所有模块共享同一实例), 通过 self.g.xxx 访问其他模块
        self.g = G
        # name 支持字符串或别名列表(列表第一个为正式名, 用于展示)
        self.name = ["mgmg"]
        # 可通过 CLI 子命令调用的方法列表: sun mgmg run / sun mgmg stop
        self.run_methods = ["run", "stop"]
        # call_is_runnig 为True时, call方法正在运行中, 不会被重复调用
        self.call_is_runnig = False
        # run_is_runnig 为True时, run方法正在运行中, 不会被重复调用
        self.run_is_runnig = False

        self.root = None
        self.dm_win = None
        self.text_dm = None
        self.dm_question_win = None
        self.dm_tools_frame = None

        self.pressed = False
        self.press_x = 0
        self.press_y = 0
        self.drag_start_x = 0
        self.drag_start_y = 0
        self.click_count = 0
        self.pending_job = None
        self.is_dragging = False

    # call方法会在程序启动时自动调用运行一次
    # 用于初始化模块或默认常驻服务
    def call(self):
        if self.call_is_runnig:
            self.g.log.run("mgmg 已启动, 跳过重复初始化")
            return

        # 注册 GUI 后台服务到 cmd(任何入口都注册, 便于 CLI run/stop 统一路由; 是否自启由 P0 按 config 决定)
        self._register_service()

        # 只有 cmd 拉起的服务子进程才创建窗口; 其余入口(CLI/P0)仅注册, 不开窗
        if not self._is_service_child():
            self.call_is_runnig = True
            return

        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception as e:
            self.g.log.run(f"mgmg DPI适配失败: {e}")

        # 主窗口(桌宠本体)
        self.root = TkinterDnD.Tk()
        self.root.overrideredirect(True)
        self.root.attributes('-alpha', 0.6)
        self.root.attributes('-topmost', True)
        self._set_transparent(self.root)
        self.root.resizable(False, False)
        self.root.geometry('150x150-6-6')

        # 子窗口(气泡)
        self.dm_win = tk.Toplevel(self.root)
        self.dm_win.overrideredirect(True)
        self.dm_win.attributes('-alpha', 0.85)
        self.dm_win.attributes('-topmost', True)
        self._set_transparent(self.dm_win)
        self.dm_win.config(bg='#123456')
        self.dm_win.geometry('-38-180')
        self.dm_win.withdraw()

        # 鼠标事件与拖放绑定
        self.root.bind('<Button-1>', self.on_press)
        self.root.bind('<B1-Motion>', self.on_drag)
        self.root.bind('<ButtonRelease-1>', self.on_release)
        self.root.bind('<Button-3>', lambda e: self.tools(e))
        self.root.drop_target_register(DND_FILES, DND_TEXT)
        self.root.dnd_bind('<<Drop>>', self.on_drop)

        # 加载表情资源并播放默认表情, 气泡默认隐藏(有内容时才显示)
        self._load_emo()
        self._emo(1)
        self.dm_hide()

        self.call_is_runnig = True
        self.g.log.run("mgmg 初始化完成")

    def run(self, name: str = ""):
        """打开GUI主循环; 是否该开由 P0 的唤醒决定, 这里只管把窗口跑起来"""
        # 显式打开即视为希望下次也自动启动
        self._set_auto_start(True)

        if self.root is None:
            return
        if self.run_is_runnig:
            self.g.log.run(f"{display_name(self)} 已通过run方法关闭")
            self.close()
            self.run_is_runnig = False
        else:
            self.run_is_runnig = True
            try:
                self.root.mainloop()
            finally:
                self.run_is_runnig = False
                self._release_service_pid()

    def stop(self):
        """sun mgmg stop: 禁止下次自动启动, 并通过 cmd 结束后台运行的桌宠"""
        self._set_auto_start(False)
        # CLI 路径不会调用 call(), 这里补注册, 保证 cmd 认得该服务(含 marker 校验)
        self._register_service()
        if self._is_running():
            self.g.cmd.stop_service(SERVICE_NAME)
        else:
            print("[sun] 未找到运行中的桌宠进程")
        if self.root is not None:
            self.close()

    # ---------- 后台服务(cmd 统一管理) ----------

    def _register_service(self):
        """向 cmd 注册 GUI 后台服务: 由 cmd 拉起独立 sun_cli 进程打开窗口(身份标识由 cmd 注入)"""
        try:
            sun_cli = Path(__file__).resolve().parent.parent / "sun_cli.py"
            self.g.cmd.register_service(
                SERVICE_NAME,
                [sys.executable, str(sun_cli), display_name(self), "run"],
                marker="sun_cli.py",
            )
        except Exception as e:
            self.g.log.run(f"{display_name(self)} 服务注册失败: {e}")

    def _is_running(self) -> bool:
        """GUI 服务是否正在后台运行(以 cmd 的 PID 文件为准)"""
        try:
            return bool(self.g.cmd.service_status(SERVICE_NAME).get("running"))
        except Exception:
            return False

    @staticmethod
    def _is_service_child() -> bool:
        """当前进程是否为 cmd 拉起的 GUI 服务子进程"""
        return bool(os.environ.get(SERVICE_ENV))

    def _release_service_pid(self):
        """GUI 正常退出时清理 data/services/mgmg.pid"""
        try:
            self.g.cmd.release_service_pid(SERVICE_NAME, os.getpid())
        except Exception:
            pass

    def _set_auto_start(self, enabled: bool):
        """写入 config.json 的 mgmg_auto_start 开关"""
        try:
            self.g.config.set("mgmg_auto_start", enabled)
            self.g.log.run(f"{display_name(self)} 自动启动已{'开启' if enabled else '关闭'}")
        except Exception as e:
            self.g.log.run(f"设置自动启动失败: {e}")

    @staticmethod
    def is_widget_alive(widget):
        if widget is None:
            return False
        try:
            return widget.winfo_exists()
        except Exception:
            return False

    @staticmethod
    def _set_transparent(widget):
        """设置窗口透明色(仅Windows支持, 其它平台会抛TclError, 忽略即可)"""
        try:
            widget.wm_attributes('-transparentcolor', '#123456')
        except Exception:
            pass

    # ---------- emo 相关辅助(走 self.g.emo 模块) ----------

    def _load_emo(self):
        emo_mod = getattr(self.g, 'emo', None)
        if emo_mod is None or not hasattr(emo_mod, 'load_emo'):
            return
        try:
            emo_mod.load_emo(self.g)
        except Exception as e:
            self.g.log.run(f"mgmg 表情资源加载失败: {e}")

    def _emo(self, number=1):
        emo_mod = getattr(self.g, 'emo', None)
        if emo_mod is not None and hasattr(emo_mod, 'emo'):
            try:
                emo_mod.emo(self.g, self, number)
            except Exception as e:
                self.g.log.run(f"mgmg 表情播放失败: {e}")

    def _emo_close(self):
        emo_mod = getattr(self.g, 'emo', None)
        if emo_mod is not None and hasattr(emo_mod, 'emo_close'):
            try:
                emo_mod.emo_close(self.g, self)
            except Exception as e:
                self.g.log.run(f"mgmg 表情关闭失败: {e}")

    # ---------- 气泡窗口管理 ----------

    def dm_has_content(self):
        """气泡内是否有实际内容(文字/工具面板/问题框/剪贴板面板已展开), 无内容不显示空气泡"""
        return (
            self.is_widget_alive(self.text_dm)
            or self.is_widget_alive(self.dm_tools_frame)
            or self.is_widget_alive(self.dm_question_win)
            or (getattr(self, 'cv_btn_fam', None) is not None
                and self.cv_btn_fam.winfo_manager())
        )

    def dm_position(self):
        """把气泡定位到主窗口正上方居中; 等窗口布局完成后一帧再取实际尺寸, 避免错位"""
        if not self.is_widget_alive(self.dm_win) or not self.is_widget_alive(self.root):
            return

        def _place():
            try:
                if not self.is_widget_alive(self.dm_win) or not self.is_widget_alive(self.root):
                    return
                self.dm_win.update_idletasks()
                root_x = self.root.winfo_x()
                root_y = self.root.winfo_y()
                root_w = self.root.winfo_width()
                dm_w = self.dm_win.winfo_width()
                dm_h = self.dm_win.winfo_height()
                dm_x = root_x + (root_w - dm_w) // 2
                dm_y = root_y - dm_h - 6
                self.dm_win.geometry(f"+{dm_x}+{dm_y}")
            except Exception:
                pass

        # 延迟到窗口完成映射与布局后再计算, 尺寸才准确
        self.dm_win.after(30, _place)


    def dm_hide(self):
        if not self.is_widget_alive(self.dm_win):
            return
        if self.dm_has_content():
            return
        self.dm_position()
        self.dm_win.withdraw()

    def dm_show(self):
        if not self.is_widget_alive(self.dm_win):
            return
        if not self.dm_has_content():
            return  # 无内容时避免显示空白蓝色气泡
        self.dm_win.deiconify()
        self.dm_position()

    def dm_text_close(self, e=None):
        if self.text_dm is not None:
            self.text_dm.destroy()
            self.text_dm = None
            self.dm_hide()
        if self.is_widget_alive(self.dm_win):
            self.dm_position()

    def dm_print(self, text, dm_print_time):
        if not self.is_widget_alive(self.dm_win):
            return
        self.dm_text_close()

        self.text_dm = tk.Label(self.dm_win, text=text, font=('微软雅黑', 12), wraplength=130,
                                bg='#757575', fg='#000000', padx=10, pady=5)
        self.text_dm.bind('<Double-Button-1>', self.dm_text_close)
        self.text_dm.pack(fill=tk.BOTH, expand=True)

        if dm_print_time == 0:
            dm_print_time = 30000
        self.text_dm.after(dm_print_time, self.dm_text_close)

        if self.is_widget_alive(self.dm_question_win):
            self.dm_question_win.destroy()

        # 等一帧后再定位和显示, 确保布局完成
        self.dm_win.after(1, lambda: (self.dm_position(), self.dm_show()))

    def t_print(self, text: str, duration: int = 3000) -> None:
        self.dm_print(text, duration)

    # ---------- 窗口缩放 ----------

    def mgmg_max(self, e=None):
        self.root.geometry('150x150')
        self.dm_show()

    def mgmg_min(self, e=None):
        if self.is_widget_alive(self.dm_tools_frame):
            self.dm_tools_frame.destroy()
            self.dm_position()
            self.dm_hide()
        if self.is_widget_alive(self.dm_question_win):
            self.dm_question_win.destroy()
        self.dm_text_close()
        self._emo_close()
        self.root.geometry('30x30-6-60')
        self.dm_show()

    # ---------- 右键工具面板 ----------

    def tools(self, e=None):
        # 已打开则关闭
        if self.is_widget_alive(self.dm_tools_frame):
            self.dm_tools_frame.destroy()
            self.dm_position()
            self.dm_hide()
            return
        if self.is_widget_alive(self.dm_question_win):
            self.dm_question_win.destroy()
        self.dm_text_close()
        self.dm_position()

        # 收集模块的 tool() 方法
        tool_modules = [inst for inst in self.g._modules.values()
                        if callable(getattr(inst, 'tool', None))]

        self.dm_tools_frame = tk.Frame(self.dm_win, bg='#123456')
        self.dm_tools_frame.pack(fill=tk.BOTH, expand=True)

        # 为模块的 tool() 方法创建按钮(回调包一层 try/except, 失败不崩 GUI)
        for inst in tool_modules:
            # 声明 tool_self_managed 的模块自己往面板里放按钮(如 lobo), 这里不再自动生成
            if getattr(inst, "tool_self_managed", False):
                try:
                    inst.tool()
                except Exception as e:
                    self.g.log.run(f"tool() 自管理面板失败 [{inst.name}]: {e}")
                continue

            display = display_name(inst)

            def _run_tool(inst=inst, display=display):
                try:
                    inst.tool()
                except Exception as e:
                    self.dm_print(f"工具 {display} 执行失败: {e}", 3000)
                    self.g.log.run(f"tool() 执行失败 [{display}]: {e}")

            ttk.Button(self.dm_tools_frame, text=display,
                       command=_run_tool).pack(fill=tk.X, padx=0, pady=4)

        ttk.Button(self.dm_tools_frame, text='关闭梦梦...', command=self.close).pack(fill=tk.X, padx=0, pady=0)
        # dm_show 内部会延迟定位, 等布局完成后再对齐主窗口
        self.dm_show()

    # ---------- 工具进度气泡（tool_print 系列） ----------
    # tool_print 相关状态（tool 模块可用）
    _tool_popup: tk.Label = None
    _tool_popup_close_cb: callable = None

    def tool_print(self, text: str, on_close: callable = None):
        """显示持续进度气泡（不自动关闭），右键取消时调用 on_close()"""
        if not self.is_widget_alive(self.dm_tools_frame):
            return
        self._tool_popup_close_cb = on_close

        if self.is_widget_alive(self._tool_popup):
            self._tool_popup.destroy()

        self._tool_popup = tk.Label(
            self.dm_tools_frame, text=text, font=("微软雅黑", 10), fg="#ffd700",
            bg="#2e2e2e", padx=8, pady=4,
        )
        self._tool_popup.pack(fill=tk.X, padx=0, pady=(4, 0), before=self.dm_tools_frame.pack_slaves()[-1])
        self._tool_popup.bind("<Button-3>", self._tool_popup_on_rightclick)
        self.dm_show()

    def _tool_popup_on_rightclick(self, e):
        """右键取消：销毁本气泡、触发回调、阻止冒泡、重新定位面板"""
        cb, self._tool_popup_close_cb = self._tool_popup_close_cb, None
        popup = self._tool_popup
        self._tool_popup = None
        try:
            popup.destroy()
        except Exception:
            pass
        if callable(cb):
            try:
                cb()
            except Exception:
                pass
        # 阻止冒泡到 dm_tools_frame，避免触发父级事件导致空模板
        try:
            e.widget.unbind("<Button-3>")
        except Exception:
            pass
        self.dm_position()
        self.dm_show()

    def tool_print_update(self, text: str):
        """更新进度气泡文字"""
        if self.is_widget_alive(self._tool_popup):
            self._tool_popup.config(text=text)
            self.dm_show()

    def tool_print_close(self):
        """外部主动关闭进度气泡（由 lobo 传输完成调用）"""
        cb, self._tool_popup_close_cb = self._tool_popup_close_cb, None
        if self.is_widget_alive(self._tool_popup):
            self._tool_popup.destroy()
            self._tool_popup = None
        if callable(cb):
            try:
                cb()
            except Exception:
                pass

    # ---------- 鼠标拖拽 ----------

    def on_press(self, event):
        self.pressed = True
        self.press_x = event.x
        self.press_y = event.y
        self.drag_start_x = event.x
        self.drag_start_y = event.y
        self.is_dragging = False

    def on_drag(self, event):
        if not self.pressed:
            return

        threshold = getattr(self.g, 'drag_threshold', 5)
        if (abs(event.x - self.press_x) <= threshold and
                abs(event.y - self.press_y) <= threshold):
            return

        self.is_dragging = True

        if self.pending_job is not None:
            self.root.after_cancel(self.pending_job)
            self.pending_job = None
            self.click_count = 0

        x = self.root.winfo_x() + (event.x - self.drag_start_x)
        y = self.root.winfo_y() + (event.y - self.drag_start_y)
        self.root.geometry(f"+{x}+{y}")

        root_w = self.root.winfo_width()
        dm_w = self.dm_win.winfo_width()
        dm_h = self.dm_win.winfo_height()
        self.dm_win.geometry(f"+{x + (root_w - dm_w) // 2}+{y - dm_h - 4}")

    def on_release(self, event):
        self.pressed = False
        if self.is_dragging:
            self.is_dragging = False
            self._emo(1)
            return

        self.click_count += 1
        if self.pending_job is not None:
            self.root.after_cancel(self.pending_job)

        def finalize():
            cnt = self.click_count
            self.click_count = 0
            self.pending_job = None

            if cnt == 1:
                self.left_click_1()
            elif cnt == 2:
                self.left_click_2()
            elif cnt == 3:
                self.left_click_3()

        self.pending_job = self.root.after(getattr(self.g, 'double_ms', 300), finalize)

    # ---------- 单击/双击/三击 行为 ----------

    def left_click_1(self):
        """单击: 展开并显示平静表情"""
        self.mgmg_max()
        self._emo(0)

    def left_click_2(self):
        """双击: 展开并显示开心表情"""
        self.mgmg_max()
        self._emo(1)

    def left_click_3(self):
        """三击: 最小化"""
        self.mgmg_min()

    def on_drop(self, event):
        """拖入文件/文本: 展开桌宠并显示开心表情"""
        self.mgmg_max()
        self._emo(1)

    # ---------- 关闭 ----------

    def close(self):
        """关闭GUI窗口"""
        # 停止emo播放
        try:
            self._emo_close()
        except Exception as e:
            self.g.log.run(f"{display_name(self)} GUI关闭 关闭emo播放失败: {e}")

        # 保存配置
        try:
            self.g.config.save()
            self.g.log.run(f"{display_name(self)} GUI关闭 保存配置成功")
        except Exception as e:
            self.g.log.run(f"{display_name(self)} GUI关闭 保存配置失败: {e}")

        # 安全销毁窗口
        try:
            if self.dm_win is not None:
                try:
                    self.dm_win.destroy()
                except Exception as e:
                    self.g.log.run(f"{display_name(self)} GUI关闭 关闭dm_win失败: {e}")

            for item in self.root.winfo_children():
                try:
                    item.destroy()
                except Exception as e:
                    self.g.log.run(f"{display_name(self)} GUI关闭 关闭子组件失败: {e}")
            tk.Label(self.root, text='good-bey', bg='#F5F5F5').pack()
            self.root.after(150, self.root.quit)
        except Exception as e:
            self.g.log.run(f"{display_name(self)} GUI关闭 关闭窗口失败: {e}")
            self.root.quit()