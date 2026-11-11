"""core 工具: 常驻进程管理

core 启动时创建一个实例挂到 G.server; core 退出或收到关闭信号时调用 shutdown()。
"""

import os
import signal
import subprocess
import time
from pathlib import Path

APP_PATH = Path(__file__).resolve().parents[1]
SERVICES_DIR = APP_PATH / "core" / "services"


class Server:
    """托管后台进程: 模块注册, 本工具负责拉起、查状态、停止"""

    def __init__(self):
        self._reg = {}

    def register(self, name: str, argv: list, marker: str = "", env: dict = None,
                 persist: bool = False):
        """登记后台进程; 只登记, 不启动。persist=True 时 core 退出后保留。"""
        self._reg[name] = {"argv": list(argv), "marker": marker or "",
                           "env": dict(env or {}), "persist": bool(persist)}

    @staticmethod
    def identity():
        """当前进程被托管时的服务名, 非托管进程返回 None"""
        return os.environ.get("SUN_SERVICE") or None

    @staticmethod
    def _pid_path(name: str) -> Path:
        return SERVICES_DIR / f"{name}.pid"

    def _read_pid(self, name: str):
        try:
            return int(self._pid_path(name).read_text().strip())
        except Exception:
            return None

    @staticmethod
    def _alive(pid: int) -> bool:
        if os.name == "nt":
            import ctypes
            kernel32 = ctypes.windll.kernel32
            handle = kernel32.OpenProcess(0x1000, False, pid)
            if not handle:
                return False
            try:
                code = ctypes.c_ulong()
                kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
                return code.value == 259  # STILL_ACTIVE
            finally:
                kernel32.CloseHandle(handle)
        try:
            os.kill(pid, 0)
            return True
        except PermissionError:
            return True
        except OSError:
            return False

    @staticmethod
    def _matches(pid: int, marker: str) -> bool:
        """cmdline 校验, 防止 PID 复用后误停无关进程; 平台不支持时放行"""
        if not marker:
            return True
        try:
            raw = Path(f"/proc/{pid}/cmdline").read_bytes()
        except FileNotFoundError:
            return False
        except OSError:
            return True
        return marker.encode() in raw.replace(b"\0", b" ")

    def status(self, name: str) -> dict:
        """{"registered": bool, "running": bool, "pid": int|None}"""
        reg = self._reg.get(name)
        if reg is None:
            return {"registered": False, "running": False, "pid": None}
        pid = self._read_pid(name)
        running = pid is not None and self._alive(pid) and self._matches(pid, reg["marker"])
        return {"registered": True, "running": running, "pid": pid if running else None}

    def start(self, name: str) -> bool:
        """拉起已登记的进程; 未登记或已在运行返回 False"""
        reg = self._reg.get(name)
        if reg is None or self.status(name)["running"]:
            return False
        SERVICES_DIR.mkdir(parents=True, exist_ok=True)
        env = {**os.environ, "SUN_DAEMON": "1", "SUN_SERVICE": name, **reg["env"]}
        kwargs = {"start_new_session": True} if os.name == "posix" else {}
        with open(SERVICES_DIR / f"{name}.log", "ab") as log:
            proc = subprocess.Popen(
                reg["argv"], cwd=str(APP_PATH), env=env,
                stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, **kwargs,
            )
        self._pid_path(name).write_text(str(proc.pid), encoding="utf-8")
        return True

    def wait(self, name: str, timeout: float = 5.0, probe=None) -> bool:
        """等待进程存活且 probe() 返回真; 超时或提前退出返回 False。"""
        deadline = time.time() + timeout
        while time.time() < deadline:
            if not self.status(name)["running"]:
                return False
            if probe is None:
                return True
            try:
                if probe():
                    return True
            except Exception:
                pass
            time.sleep(0.1)
        return False

    def stop(self, name: str) -> bool:
        """停止进程: 先 SIGTERM, 1.5 秒未退出则强杀"""
        if not self.status(name)["running"]:
            self._pid_path(name).unlink(missing_ok=True)
            return False
        pid = self._read_pid(name)
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(pid), "/F"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
            try:
                os.kill(pid, signal.SIGTERM)
            except OSError:
                pass
            for _ in range(15):
                if not self._alive(pid):
                    break
                time.sleep(0.1)
            else:
                try:
                    os.kill(pid, signal.SIGKILL)
                except OSError:
                    pass
        self._pid_path(name).unlink(missing_ok=True)
        return True

    def stop_all(self):
        """停止所有非 persist 的托管进程"""
        for name, reg in list(self._reg.items()):
            if reg.get("persist"):
                continue
            try:
                self.stop(name)
            except Exception:
                pass

    def list(self) -> list:
        """[(名字, 是否运行, pid), ...]"""
        rows = []
        for name in self._reg:
            st = self.status(name)
            rows.append((name, st["running"], st["pid"]))
        return rows

    def shutdown(self):
        """core 退出/收到关闭信号时停止全部非 persist 进程"""
        self.stop_all()
