"""
cmd 命令中枢模块

功能:
    1. 调用统计: 记录各模块 run / 子命令 被使用的次数(直接读写 config.json 的 cmd_usage 键)
    2. sun list: 按调用次数降序列出所有模块及其可调用子命令
    3. sun <模块> list: 只列出指定模块的可调用子命令

用法:
    sun list             # 列出所有模块及其子命令(常用命令排前面)
    sun mgmg list        # 列出 mgmg 的可调用子命令(如 run / stop)
    sun cmd list         # 同 sun list

约定:
    - run:  CLI 入口, 模块声明非空 run_methods 即成为 CLI 命令(sun <模块> ...)
    - tool: GUI 右键工具入口, 模块实现 tool() 即出现在桌宠右键工具面板
    - 本模块无需 call() 初始化: 统计与工具列表都在使用时现取, 不缓存别人的状态

后台服务管理:
    - 各模块在 call() 中 register_service(名字, argv, marker) 注册自己的后台服务(只提供定义)
    - cmd 统一登记并通过 start_service / stop_service / service_status 路由启停
    - PID 文件统一放在 data/services/<名字>.pid, 服务之间互不影响
    - sun <模块> run / sun <模块> stop 由 sun_cli 分发到模块, 再委托这里执行

服务自启(P0 唤醒契约):
    - 定义在模块(register_service), 策略在 config.json: <名字>_auto_start, 缺省为 True
    - P0(sun_cli 无参数入口)只调 autostart_services(), 不认任何具体服务名
    - 新增服务 = 新增 mudels/<名字>.py 并在 call() 里注册, P0 与 config 都不必改动
    - 不做崩溃监控: 服务退出后不会自动拉起, 下次 P0 唤醒时按 config 与运行状态补齐
    - 服务子进程由 cmd 统一注入 SUN_DAEMON=1 / SUN_SERVICE=<名字> 标识身份
"""

import os
import subprocess
from pathlib import Path

import psutil

from base import display_name

# 统计在 config.json 中的键名
USAGE_KEY = "cmd_usage"

# 后台服务 PID 文件目录(相对项目根, sun 启动时 cwd 即项目根)
SERVICE_DIR = Path("data/services")

# 服务子进程身份标识: 前者表示"我是 cmd 拉起的服务进程", 后者记录具体是哪个服务
SERVICE_ENV = "SUN_DAEMON"
SERVICE_NAME_ENV = "SUN_SERVICE"


class Cmd:
    # g 是全局对象, 其中挂载着所有模块的实例,用于实现模块之间的通信与调用、重载
    def __init__(self, G):
        # g 为全局对象引用(所有模块共享同一实例), 通过 self.g.xxx 访问其他模块
        self.g = G
        # name 支持字符串或别名列表: sun list 通过别名 "list" 也能命中本模块
        self.name = ["cmd", "list"]
        # 可通过 CLI 子命令调用的方法列表: sun cmd list
        self.run_methods = ["list"]
        # 后台服务注册表: {服务名: {"argv": [启动命令...], "marker": cmdline 校验串}}
        self._services: dict = {}

    def _usage(self) -> dict:
        """调用统计: 每次使用时从 config 现取, 不在实例上缓存(避免依赖 config 的初始化顺序)"""
        try:
            return self.g.config.get(USAGE_KEY, {}) or {}
        except Exception:
            return {}

    def record(self, module: str, method: str):
        """记录一次调用, 直接写回 config.json 的 cmd_usage 键"""
        usage = self._usage()
        d = usage.setdefault(module, {})
        d[method] = d.get(method, 0) + 1
        try:
            self.g.config.set(USAGE_KEY, usage)
        except Exception:
            pass

    def count(self, module: str) -> int:
        """模块累计调用次数(所有方法之和)"""
        return sum(self._usage().get(module, {}).values())

    # ---------- 后台服务管理 ----------

    def register_service(self, name: str, argv: list, marker: str = "", env: dict = None):
        """注册一个后台服务(幂等, 重复注册以最后一次为准); 只提供定义, 不决定是否启动

        name:   服务名(如 "lobo"), 同时也是 config 中自启开关 <name>_auto_start 的前缀
        argv:   启动命令(list), 以项目根为工作目录
        marker: 用于进程 cmdline 校验的字符串, 防止 PID 复用后误杀无关进程
        env:    启动时额外注入的环境变量; SUN_DAEMON / SUN_SERVICE 由 cmd 统一注入

        注意: 服务需要 G/config 时, argv 应经统一入口 sun_cli.py, 以便服务进程内也完成模块加载
        """
        service_env = {SERVICE_ENV: "1", SERVICE_NAME_ENV: name}
        service_env.update(env or {})
        self._services[name] = {"argv": list(argv), "marker": marker or "", "env": service_env}

    @staticmethod
    def service_identity():
        """当前进程所属的后台服务名; 非服务子进程(如 P0 / CLI)返回 None"""
        return os.environ.get(SERVICE_NAME_ENV) or None

    def _pid_file(self, name: str) -> Path:
        return SERVICE_DIR / f"{name}.pid"

    @staticmethod
    def _pid_alive(pid: int) -> bool:
        """进程是否存活(psutil 跨平台, 兼容 Windows 不存在 SIGKILL 的场景)"""
        try:
            psutil.Process(pid).status()
            return True
        except psutil.NoSuchProcess:
            return False
        except Exception:
            # 进程存在但无权限读取时按存活处理(与旧的 os.kill(pid,0) 的 PermissionError→True 一致)
            return True

    @staticmethod
    def _pid_matches(pid: int, marker: str) -> bool:
        """校验进程的 cmdline 是否包含 marker(PID 被复用时不误杀无关进程)

        psutil 跨平台读取进程命令行, 替代 Linux 专属的 /proc/<pid>/cmdline
        """
        if not marker:
            return True
        try:
            return marker in " ".join(psutil.Process(pid).cmdline())
        except (psutil.NoSuchProcess, psutil.AccessDenied, Exception):
            return False

    def _read_pid(self, name: str):
        try:
            return int(self._pid_file(name).read_text().strip())
        except Exception:
            return None

    def _write_pid(self, name: str, pid: int):
        try:
            SERVICE_DIR.mkdir(parents=True, exist_ok=True)
            self._pid_file(name).write_text(str(pid))
        except Exception as e:
            self._log(f"写服务 PID 文件失败 [{name}]: {e}")

    def _remove_pid(self, name: str):
        try:
            self._pid_file(name).unlink(missing_ok=True)
        except Exception:
            pass

    def _log(self, text: str):
        try:
            self.g.log.run(text)
        except Exception:
            pass

    def service_status(self, name: str) -> dict:
        """查询服务状态: {"registered": bool, "running": bool, "pid": int|None}"""
        svc = self._services.get(name)
        if svc is None:
            return {"registered": False, "running": False, "pid": None}
        pid = self._read_pid(name)
        if pid is not None and self._pid_alive(pid) and self._pid_matches(pid, svc["marker"]):
            return {"registered": True, "running": True, "pid": pid}
        return {"registered": True, "running": False, "pid": None}

    def start_service(self, name: str) -> bool:
        """后台启动服务; 已在运行返回 False(幂等)"""
        svc = self._services.get(name)
        if svc is None:
            self._log(f"后台服务未注册: {name}")
            return False
        if self.service_status(name)["running"]:
            return False
        try:
            SERVICE_DIR.mkdir(parents=True, exist_ok=True)
            proc = subprocess.Popen(
                svc["argv"], cwd=os.getcwd(),
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                start_new_session=True,
                env={**os.environ, **(svc.get("env") or {})},
            )
        except Exception as e:
            self._log(f"后台服务启动失败 [{name}]: {e}")
            return False
        self._write_pid(name, proc.pid)
        self._log(f"后台服务已启动: {name} (pid={proc.pid})")
        return True

    def stop_service(self, name: str) -> bool:
        """停止后台服务; 未在运行返回 False(幂等)"""
        if not self.service_status(name)["running"]:
            self._remove_pid(name)
            return False
        pid = self._read_pid(name)
        proc = None
        try:
            proc = psutil.Process(pid)
        except psutil.NoSuchProcess:
            proc = None
        if proc is None:
            self._remove_pid(name)
            return False
        try:
            proc.terminate()  # 优先给正常退出信号(Windows 等价 TerminateProcess)
        except Exception as e:
            self._log(f"停止后台服务失败 [{name}]: {e}")
            self._remove_pid(name)
            return False
        # 等待 1.5s 仍未退出则强制结束
        try:
            proc.wait(timeout=1.5)
        except Exception:
            try:
                proc.kill()  # 强制结束(psutil 跨平台, 替代 Linux 专属 SIGKILL)
                proc.wait(timeout=2)
            except Exception:
                pass
        self._remove_pid(name)
        self._log(f"后台服务已停止: {name} (pid={pid})")
        return True

    def list_services(self) -> list:
        """列出已注册服务: [(名字, 是否运行, pid), ...]"""
        return [(n, self.service_status(n)["running"], self.service_status(n)["pid"])
                for n in self._services]

    # ---------- 自启唤醒(P0 专用) ----------

    def auto_start_enabled(self, name: str) -> bool:
        """服务是否允许开机自启: 读 config 的 <名字>_auto_start, 缺省允许"""
        try:
            return bool(self.g.config.get(f"{name}_auto_start", True))
        except Exception:
            return True

    def autostart_services(self) -> list:
        """P0 唤醒: 遍历已注册服务, 拉起未运行且允许自启的服务, 返回被拉起者的名字列表

        只认注册表与 config 开关, 不认识任何具体服务; 服务崩溃后不会走到这里, 也就不会重启
        """
        started = []
        for name in list(self._services):
            if self.service_status(name)["running"]:
                continue
            if not self.auto_start_enabled(name):
                self._log(f"后台服务已按配置跳过自启: {name}")
                continue
            if self.start_service(name):
                started.append(name)
        return started

    def release_service_pid(self, name: str, pid: int = None):
        """服务自身退出时清理 PID 文件(仅当文件记录的正是自己时才删, 防止误删他人)"""
        try:
            if self._read_pid(name) == (pid if pid is not None else os.getpid()):
                self._remove_pid(name)
        except Exception:
            pass

    # ---------- run ----------

    def run(self, name: str = ""):
        """sun list: 列出所有模块及其可调用子命令"""
        self.list()

    def list(self, module_inst=None, name=""):
        """sun list(全部) / sun <模块> list(单个)"""
        if module_inst is not None:
            self._print_one(module_inst)
        else:
            self._print_all()

    # ---------- 列表输出 ----------

    def _rows(self):
        """收集有 CLI 命令的模块(声明了非空 run_methods): (展示名, 可调用方法列表, 实例)"""
        rows = []
        for file_name, inst in self.g._modules.items():
            if not hasattr(inst, "g"):
                continue  # 无类模块(如 emo.py)不参与命令分发
            methods = getattr(inst, "run_methods", None)
            if not methods:
                continue  # 未声明 run_methods 的模块(如 log/config)不是 CLI 命令
            display = display_name(inst, file_name)
            rows.append((display, methods, inst))
        return rows

    def _print_all(self):
        """列出所有可调用命令, 按调用次数降序(未调用过的按名称排序)"""
        rows = self._rows()
        rows.sort(key=lambda r: (-self.count(r[0]), r[0]))
        print("可用命令:")
        for display, methods, _inst in rows:
            line = f"  sun {display}"
            if methods:
                line += "  →  " + " / ".join(methods)
            print(line)

    def _print_one(self, inst):
        """列出单个模块的可调用子命令"""
        display = display_name(inst, getattr(inst, "name", "?"))
        methods = getattr(inst, "run_methods", []) or []
        print(f"{display} 可调用命令:")
        if methods:
            for m in methods:
                print(f"  sun {display} {m}")
        else:
            print(f"  (模块 {display} 没有可调用命令)")
