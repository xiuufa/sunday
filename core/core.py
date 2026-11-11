#!/usr/bin/env python3
"""Sunday core

core/core.py --daemon          常驻: 挂载三件套、按 init.json 初始化、监听 UDS
core/core.py --client [指令]   客户端: 连接(必要时拉起) core 并转发指令

core 只做四件事:
    1. G / T 不存在时创建它们
    2. 挂载 log / init / server 三件套
    3. 把空参交给 init, 由 init 按 core/init.json 初始化
    4. 把 sun 的指令穿透给模块; 未初始化就提示, 不自动初始化

模块位于 modules/<名字>/<名字>.py, 导出 Module 类; 导入前注入 G / T。
初始化 = G.<模块> = 实例 + 调用模块的 init()。
同名 <名字>.json 提供 ID, 同 role 模块按 ID 时间戳大者胜出。
role 是主键, name 是别名; used=False 的模块不开放给 sun。
"""

import contextlib
import importlib.util
import io
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path

from init import Initializer
from log import write as write_log
from server import Server

try:
    import fcntl
except ImportError:  # Windows
    fcntl = None

try:
    import msvcrt
except ImportError:  # Linux
    msvcrt = None

CORE_DIR = Path(__file__).resolve().parent
APP_PATH = CORE_DIR.parent
# 保证 core 工具可导入, 同时模块可以用 from modules.<名字> import <辅助文件>
if str(CORE_DIR) not in sys.path:
    sys.path.insert(0, str(CORE_DIR))
if str(APP_PATH) not in sys.path:
    sys.path.insert(0, str(APP_PATH))

MODULES_DIR = APP_PATH / "modules"
SOCKET_PATH = CORE_DIR / "core.sock"
LOCK_PATH = CORE_DIR / "core.lock"
PID_PATH = CORE_DIR / "core.pid"
CORE_LOG = CORE_DIR / "core.log"

MODULE_EXPORT = "Module"
# core 内建命令, 不参与模块路由
RESERVED_COMMANDS = {"help", "stop", "list", "init"}
# 模块请求客户端确认时的退出码
CONFIRM_EXIT_CODE = 3
# G 自身占用的属性, 不允许模块名/role 覆盖
_G_RESERVED = {
    "APP_PATH", "MODULES_LIST", "by_name", "by_role",
    "get", "call", "log", "init", "server",
}


class G:
    """全局共享对象: core 注入到每个模块的全局命名空间"""

    def __init__(self):
        self.APP_PATH = str(APP_PATH)
        self.MODULES_LIST = []
        self.by_name = {}
        self.by_role = {}

    def get(self, key, default=None):
        """按 role 或 name 取已加载模块; role 是主键, name 是别名"""
        mod = self.by_role.get(key)
        if mod is None:
            mod = self.by_name.get(key)
        return default if mod is None else mod

    def __contains__(self, key):
        return key in self.by_role or key in self.by_name


class T:
    """临时共享对象: 每次请求的上下文可以放在这里"""

    def __init__(self):
        self.CLIENT_CWD = str(APP_PATH)


_G = None
_T = None


def context():
    """G / T 不存在时创建; log 一并挂到 G.log"""
    global _G, _T
    if _G is None:
        _G = G()
        _G.log = write_log
    if _T is None:
        _T = T()
    return _G, _T


class Sunday:
    def __init__(self):
        self.G, self.T = context()
        self.G.server = Server()
        self.init = Initializer(self.G, self.load_module, _G_RESERVED)
        self.G.init = self.init
        self.G.call = self.call
        self._index = {}
        self._stop = False
        self._tools_closed = False
        self._lock_fh = None
        self._sock = None

    @staticmethod
    def _system_message(message: str):
        """调用系统消息弹窗; 没有可用 GUI 工具时回退到 stderr"""
        title = "Sunday core"
        try:
            if sys.platform.startswith("win"):
                import ctypes
                ctypes.windll.user32.MessageBoxW(0, message, title, 0x10)
                return
            if sys.platform == "darwin":
                script = (
                    f'display dialog {json.dumps(message)} with title {json.dumps(title)} '
                    'buttons {"OK"} with icon stop'
                )
                subprocess.Popen(
                    ["osascript", "-e", script],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    start_new_session=True,
                )
                return
            # Linux: 优先用真正的对话框, 其次桌面通知
            for cmd in (
                ["zenity", "--error", "--title", title, "--text", message],
                ["kdialog", "--title", title, "--error", message],
                ["notify-send", "-u", "critical", title, message],
            ):
                if shutil.which(cmd[0]):
                    subprocess.Popen(
                        cmd,
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                        start_new_session=True,
                    )
                    return
        except Exception:
            pass
        print(f"[core] {message}", file=sys.stderr)

    @classmethod
    def _fatal(cls, message: str):
        """致命错误: 写入 fatal-only 日志 + 系统弹窗"""
        try:
            CORE_DIR.mkdir(parents=True, exist_ok=True)
            with open(CORE_LOG, "a", encoding="utf-8") as f:
                f.write(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {message}\n")
        except Exception:
            pass
        cls._system_message(message)

    def scan_modules(self):
        """只读 manifest 建立索引, 启动时不导入模块"""
        self._index = {}
        if not MODULES_DIR.is_dir():
            return

        for mod_dir in sorted(MODULES_DIR.iterdir()):
            if not mod_dir.is_dir() or mod_dir.name.startswith("_"):
                continue
            path = mod_dir / f"{mod_dir.name}.py"
            if not path.is_file():
                continue
            entry = self._manifest_entry(mod_dir.name, path)
            for token in {entry["dir"], entry["role"], entry["name"]}:
                if not token:
                    continue
                old = self._index.get(token)
                if old is None or self._entry_timestamp(entry) > self._entry_timestamp(old):
                    self._index[token] = entry

    @staticmethod
    def _manifest_entry(dir_name: str, path: Path) -> dict:
        """manifest 与模块 py 同名: modules/example/example.json"""
        manifest = {}
        try:
            data = json.loads(path.with_suffix(".json").read_text(encoding="utf-8"))
            if isinstance(data, dict):
                manifest = data
        except Exception:
            pass
        role = str(manifest.get("role") or dir_name).strip() or dir_name
        name = str(manifest.get("name") or role).strip() or role
        used = manifest.get("used")
        return {
            "dir": dir_name,
            "path": path,
            "role": role,
            "name": name,
            "ID": str(manifest.get("ID") or ""),
            "desc": str(manifest.get("desc") or manifest.get("help") or ""),
            "used": used if isinstance(used, bool) else True,
            "init": manifest.get("init") is True,
        }

    @staticmethod
    def _entry_timestamp(entry: dict) -> int:
        raw = str(entry.get("ID") or "")
        for prefix in (entry.get("name", ""), entry.get("role", "")):
            prefix = str(prefix or "")
            if prefix and raw.startswith(prefix):
                raw = raw[len(prefix):]
                break
        match = re.search(r"(\d+)$", raw)
        return int(match.group(1)) if match else -1

    def load_module(self, token: str):
        """按需加载: 只导入/实例化, 不调用模块的 init()"""
        mod = self.resolve(token)
        if mod is not None:
            return mod
        entry = self._index.get(token)
        if entry is None:
            return None

        inst = self._instantiate(entry)
        if inst is None:
            return None
        self._register(inst)
        self.rebuild_registry()
        return self.resolve(token) or inst

    def _instantiate(self, entry: dict):
        dir_name = entry["dir"]
        path = entry["path"]
        manifest = {}
        try:
            data = json.loads(path.with_suffix(".json").read_text(encoding="utf-8"))
            if isinstance(data, dict):
                manifest = data
        except Exception:
            pass

        spec_name = f"sunday_module_{dir_name}"
        try:
            spec = importlib.util.spec_from_file_location(spec_name, path)
            if spec is None or spec.loader is None:
                raise ImportError("无法创建模块 spec")
            module = importlib.util.module_from_spec(spec)
            # 注入全局对象, 必须在 exec_module 之前
            module.G = self.G
            module.T = self.T
            sys.modules[spec_name] = module
            spec.loader.exec_module(module)
        except Exception:
            sys.modules.pop(spec_name, None)
            return None

        cls = getattr(module, MODULE_EXPORT, None)
        if not isinstance(cls, type):
            return None
        try:
            inst = cls()
        except Exception:
            return None

        # ID 以 manifest 为准; 类属性只作为没有 manifest 时的兜底
        manifest_id = manifest.get("ID")
        if isinstance(manifest_id, str) and manifest_id.strip():
            inst.ID = manifest_id.strip()

        # desc 可由类提供, 也可由 manifest 的 desc/help 兜底
        desc = getattr(inst, "desc", None)
        if not isinstance(desc, str) or not desc.strip():
            help_text = manifest.get("desc") or manifest.get("help")
            if isinstance(help_text, str):
                inst.desc = help_text

        # used 可由类属性提供, manifest 优先
        manifest_used = manifest.get("used")
        if isinstance(manifest_used, bool):
            inst._used = manifest_used

        return inst if self._normalize_instance(inst) else None

    def _register(self, inst):
        """同 role 模块: ID 时间戳大的替换旧的"""
        existing = next(
            (m for m in self.G.MODULES_LIST if getattr(m, "role", None) == inst.role),
            None,
        )
        if existing is not None:
            if self._module_timestamp(inst) <= self._module_timestamp(existing):
                return
            self.G.MODULES_LIST[self.G.MODULES_LIST.index(existing)] = inst
            return
        self.G.MODULES_LIST.append(inst)

    def _normalize_instance(self, inst) -> bool:
        """统一模块实例属性; role 必填, name 是可选别名"""
        role = getattr(inst, "role", None)
        if not isinstance(role, str) or not role.strip():
            return False
        inst.role = role.strip()
        if inst.role in RESERVED_COMMANDS:
            return False

        name = getattr(inst, "name", None)
        name = name.strip() if isinstance(name, str) and name.strip() else inst.role
        # 别名不能占用 core 保留命令; 退回 role
        inst.name = inst.role if name in RESERVED_COMMANDS else name

        # used 可能是类属性, 也可能是同名子命令(如 market used); 内部统一用 _used
        raw_used = getattr(inst, "used", None)
        if not isinstance(getattr(inst, "_used", None), bool):
            inst._used = raw_used if isinstance(raw_used, bool) else True
        if not callable(raw_used):
            inst.used = inst._used

        if not isinstance(getattr(inst, "ID", None), str):
            inst.ID = ""

        raw_subs = getattr(inst, "subcommands", [])
        if not isinstance(raw_subs, list):
            raw_subs = []
        subcommands = []
        for sub in raw_subs:
            if not isinstance(sub, str):
                continue
            sub = sub.strip()
            if not sub or sub.startswith("_"):
                continue
            if not callable(getattr(inst, sub, None)):
                continue
            if sub not in subcommands:
                subcommands.append(sub)
        inst.subcommands = subcommands
        return True

    @staticmethod
    def _module_timestamp(mod) -> int:
        """ID = name + int(time.time()); 去掉 name/role 前缀后按数字大小比较"""
        raw = str(getattr(mod, "ID", "") or "").strip()
        for prefix in (getattr(mod, "name", ""), getattr(mod, "role", "")):
            prefix = str(prefix or "")
            if prefix and raw.startswith(prefix):
                raw = raw[len(prefix):]
                break
        match = re.search(r"(\d+)$", raw)
        return int(match.group(1)) if match else -1

    def rebuild_registry(self):
        """重建已加载模块的 name/role 索引"""
        G = self.G
        G.by_name = {}
        G.by_role = {}
        for mod in G.MODULES_LIST:
            name = getattr(mod, "name", None)
            if name:
                old = G.by_name.get(str(name))
                if old is None or self._module_timestamp(mod) > self._module_timestamp(old):
                    G.by_name[str(name)] = mod
            role = getattr(mod, "role", None)
            if isinstance(role, str) and role:
                old = G.by_role.get(role)
                if old is None or self._module_timestamp(mod) > self._module_timestamp(old):
                    G.by_role[role] = mod

    def resolve(self, token: str):
        """role 是主键, name 是别名"""
        return self.G.by_role.get(token) or self.G.by_name.get(token)

    def _entries(self) -> list:
        """按目录去重后的模块清单"""
        seen, entries = set(), []
        for entry in self._index.values():
            if entry["dir"] in seen:
                continue
            seen.add(entry["dir"])
            entries.append(entry)
        return sorted(entries, key=lambda item: item["dir"])

    def _policy(self, entry):
        policy = self.init.policy(entry["dir"], entry["role"], entry["name"])
        if policy is None:
            return None
        # 是否随 core 启动初始化以模块 JSON 的 init 为准
        return {**policy, "init": entry["init"]}

    def _init_targets(self) -> list:
        """按 init.json 顺序选出 used && 模块 JSON init=true 的模块"""
        targets = []
        for item in self.init.load():
            if not item["used"]:
                continue
            entry = self._index.get(item["name"])
            enabled = entry["init"] if entry is not None else item["init"]
            if enabled:
                targets.append(item["name"])
        return targets

    def _available(self) -> list:
        """可用模块: 过滤 used, 需要时才导入"""
        mods = []
        for entry in self._entries():
            policy = self._policy(entry)
            used = policy["used"] if policy else entry["used"]
            if not used:
                continue
            mod = self.load_module(entry["dir"])
            if mod is not None:
                mods.append(mod)
        return mods

    def cmd_list(self, simple=False):
        """列出可用模块; simple=True 时只输出 name"""
        mods = self._available()
        if not mods:
            print("(没有可用模块)")
            return
        for mod in mods:
            name = str(getattr(mod, "name", "") or getattr(mod, "role", ""))
            if simple:
                print(name)
            else:
                role = getattr(mod, "role", "")
                desc = getattr(mod, "desc", "")
                print(f"{role}\t{name}\t{desc}")

    def cmd_help(self):
        print("这里是 sunday: 基于 Python 的命令行自定义工具框架")
        print("用法:")
        print("  sun                    确保 core 运行")
        print("  sun help               显示帮助和指令列表")
        print("  sun list               列出可用指令")
        print("  sun init [模块...]     初始化指定模块, 或按 init.json 初始化")
        print("  sun stop               停止 core")
        print("  sun <指令>             调用模块的默认功能 run()")
        print("  sun <指令> <子命令> ... 调用模块的功能方法")
        print("  sun <指令> <help>      显示模块的帮助")

    def dispatch(self, argv, cwd=None):
        self.T.CLIENT_CWD = cwd or str(APP_PATH)
        if not argv:
            self.cmd_help()
            return

        head = str(argv[0])
        rest = argv[1:]

        if head == "help":
            self.cmd_help()
            return
        if head == "list":
            self.cmd_list(simple=True)
            return
        if head == "stop":
            self._stop = True
            print("core 已停止")
            return
        if head == "init":
            self.init.run(rest or self._init_targets(), quiet=False)
            return

        entry = self._index.get(head)
        policy = self._policy(entry) if entry else None
        mod = self.resolve(head)
        if mod is None and entry is None:
            raise KeyError(f"未知指令: {head}")

        used = policy["used"] if policy else (entry["used"] if entry else True)
        if not used:
            raise AttributeError(f"模块 {head} 未开放给 sun")

        if mod is None:
            mod = self.load_module(head)
        if mod is None:
            raise KeyError(f"未知指令: {head}")

        if policy and policy["init"] and not self.init.is_initialized(head, mod.role, mod.name):
            raise AttributeError(f"模块 {head} 未初始化, 先执行: sun init {head}")

        if rest:
            sub = str(rest[0])
            subs = getattr(mod, "subcommands", []) or []
            if sub not in subs:
                available = ", ".join(str(s) for s in subs) or "(无)"
                raise AttributeError(f"模块 {head} 没有子命令: {sub}; 可用: {available}")
            fn = getattr(mod, sub, None)
            if not callable(fn):
                raise AttributeError(f"模块 {head} 的子命令不可调用: {sub}")
            fn(*rest[1:])
            return

        fn = getattr(mod, "run", None)
        if not callable(fn):
            raise AttributeError(f"模块 {head} 没有 run 方法")
        fn()

    def call(self, *argv):
        """模块内执行一条 sun 指令: G.call("lobo", "get")

        返回 (code, stdout, stderr), 与客户端拿到的结果一致。
        """
        if len(argv) == 1 and isinstance(argv[0], (list, tuple)):
            argv = tuple(argv[0])
        return self.execute([str(item) for item in argv], self.T.CLIENT_CWD)

    def execute(self, argv, cwd=None):
        """执行一条命令并捕获输出, 返回 (code, stdout, stderr)"""
        out, err = io.StringIO(), io.StringIO()
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                self.dispatch(argv, cwd)
            return 0, out.getvalue(), err.getvalue()
        except SystemExit as e:
            code = e.code if isinstance(e.code, int) else (0 if e.code is None else 1)
            return code, out.getvalue(), err.getvalue()
        except (KeyError, AttributeError) as e:
            message = str(e).strip("'")
            err.write(f"{message}\n")
            err.write("用法: sun help\n")
            return 2, out.getvalue(), err.getvalue()
        except Exception:
            err.write(traceback.format_exc())
            return 1, out.getvalue(), err.getvalue()

    def acquire_lock(self) -> bool:
        CORE_DIR.mkdir(parents=True, exist_ok=True)
        fh = open(LOCK_PATH, "a+b")
        try:
            if fcntl is not None:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            elif msvcrt is not None:
                fh.seek(0)
                if not fh.read(1):
                    fh.write(b"0")
                    fh.flush()
                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            fh.close()
            return False
        self._lock_fh = fh
        return True

    def release_lock(self):
        fh, self._lock_fh = self._lock_fh, None
        if fh is None:
            return
        try:
            if fcntl is not None:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
            elif msvcrt is not None:
                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
        except Exception:
            pass
        try:
            fh.close()
        except Exception:
            pass

    def _shutdown_tools(self):
        """init / server 的关停善后; 重复调用无副作用"""
        if self._tools_closed:
            return
        self._tools_closed = True
        with contextlib.suppress(Exception):
            self.init.shutdown()
        with contextlib.suppress(Exception):
            self.G.server.shutdown()

    def _install_signals(self):
        def _on_signal(signum, _frame):
            self._shutdown_tools()
            self._stop = True

        for sig in (signal.SIGINT, signal.SIGTERM, getattr(signal, "SIGHUP", None)):
            if sig is None:
                continue
            try:
                signal.signal(sig, _on_signal)
            except Exception:
                pass

    def _handle_client(self, conn):
        try:
            with conn:
                stream = conn.makefile("rwb")
                line = stream.readline(10 * 1024 * 1024)
                if not line:
                    return
                try:
                    req = json.loads(line.decode("utf-8"))
                    if not isinstance(req, dict):
                        raise ValueError("请求必须是 JSON 对象")
                    argv = req.get("argv") or []
                    if not isinstance(argv, list):
                        raise ValueError("argv 必须是列表")
                    code, out, err = self.execute(argv, req.get("cwd"))
                except Exception as e:
                    code, out, err = 2, "", f"请求错误: {e}\n"
                resp = {"code": code, "stdout": out, "stderr": err}
                stream.write(json.dumps(resp, ensure_ascii=False).encode("utf-8") + b"\n")
                stream.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        except Exception:
            pass

    def _cleanup(self):
        self._stop = True
        self._shutdown_tools()
        if self._sock is not None:
            try:
                self._sock.close()
            except Exception:
                pass
            self._sock = None
        try:
            SOCKET_PATH.unlink(missing_ok=True)
        except Exception:
            pass
        try:
            if PID_PATH.exists() and PID_PATH.read_text().strip() == str(os.getpid()):
                PID_PATH.unlink(missing_ok=True)
        except Exception:
            pass
        self.release_lock()

    def daemon(self) -> int:
        if not self.acquire_lock():
            return 0

        self._install_signals()
        try:
            SOCKET_PATH.unlink(missing_ok=True)
        except Exception:
            pass

        try:
            self.scan_modules()
            self.init.run(self._init_targets(), quiet=True)

            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.bind(str(SOCKET_PATH))
            try:
                os.chmod(SOCKET_PATH, 0o600)
            except OSError:
                pass
            sock.listen(8)
            sock.settimeout(1.0)
            self._sock = sock

            PID_PATH.write_text(str(os.getpid()), encoding="utf-8")

            while not self._stop:
                try:
                    conn, _ = sock.accept()
                except socket.timeout:
                    continue
                except OSError:
                    break
                self._handle_client(conn)
        except Exception:
            self._fatal(f"core 启动失败:\n{traceback.format_exc()}")
            return 1
        finally:
            self._cleanup()
        return 0


def _connect(timeout: float = 0.3):
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.connect(str(SOCKET_PATH))
    except OSError:
        sock.close()
        return None
    sock.settimeout(None)
    return sock


def _spawn_core():
    CORE_DIR.mkdir(parents=True, exist_ok=True)
    with open(CORE_LOG, "ab") as log_file:
        kwargs = {"start_new_session": True} if os.name == "posix" else {}
        subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()), "--daemon"],
            cwd=str(APP_PATH),
            stdin=subprocess.DEVNULL,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            **kwargs,
        )


def _ensure_core(timeout: float = 10.0):
    sock = _connect()
    if sock is not None:
        return sock

    _spawn_core()
    deadline = time.time() + timeout
    while time.time() < deadline:
        sock = _connect()
        if sock is not None:
            return sock
        time.sleep(0.05)
    raise RuntimeError(f"core 启动超时, 请查看 {CORE_LOG}")


def _request(sock, payload):
    sock.sendall(json.dumps(payload, ensure_ascii=False).encode("utf-8") + b"\n")
    buf = bytearray()
    while True:
        chunk = sock.recv(65536)
        if not chunk:
            raise RuntimeError("core 已退出")
        buf.extend(chunk)
        if b"\n" in buf:
            break
    line = bytes(buf).split(b"\n", 1)[0]
    return json.loads(line.decode("utf-8"))


def client(argv) -> int:
    cwd = os.environ.get("SUN_CLIENT_CWD") or os.getcwd()

    # sun: 只确保 core 活着; 已在运行则直接返回
    if not argv:
        try:
            sock = _ensure_core()
            sock.close()
        except Exception as e:
            print(f"[sun] {e}", file=sys.stderr)
            return 1
        return 0

    # sun stop: core 不在时不去拉起, 直接提示
    if argv[0] == "stop":
        sock = _connect(0.3)
        if sock is None:
            print("[sun] core 未运行")
            return 0
        try:
            resp = _request(sock, {"argv": ["stop"], "cwd": cwd})
        except Exception as e:
            print(f"[sun] {e}", file=sys.stderr)
            return 1
        finally:
            sock.close()
        sys.stdout.write(resp.get("stdout") or "")
        sys.stderr.write(resp.get("stderr") or "")
        return int(resp.get("code") or 0)

    if str(argv[0]).startswith("-"):
        print(f"[sun] 未知选项: {argv[0]}", file=sys.stderr)
        print("用法: sun help", file=sys.stderr)
        return 2

    request_argv = list(argv)
    confirmed = False
    while True:
        try:
            sock = _ensure_core()
            try:
                resp = _request(sock, {"argv": request_argv, "cwd": cwd})
            finally:
                sock.close()
        except KeyboardInterrupt:
            print("\n[sun] 已取消", file=sys.stderr)
            return 130
        except Exception as e:
            print(f"[sun] {e}", file=sys.stderr)
            return 1

        sys.stdout.write(resp.get("stdout") or "")
        sys.stderr.write(resp.get("stderr") or "")
        code = int(resp.get("code") or 0)
        if code != CONFIRM_EXIT_CODE or confirmed:
            return code

        try:
            answer = input("输入 y 确认，直接回车取消: ")
        except (EOFError, KeyboardInterrupt):
            print("\n[sun] 已取消", file=sys.stderr)
            return 1
        if answer.strip().lower() != "y":
            return 1

        request_argv = list(argv) + ["--yes"]
        confirmed = True


def main() -> int:
    args = sys.argv[1:]
    if args and args[0] == "--daemon":
        return Sunday().daemon()
    if args and args[0] == "--client":
        return client(args[1:])
    return client(args)


if __name__ == "__main__":
    sys.exit(main())
