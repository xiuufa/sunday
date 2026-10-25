#!/usr/bin/env python3
"""
Sunday core

两种角色:
    core.py --daemon          常驻核心: 加载模块、初始化、监听 UDS
    core.py --client [指令]   短命客户端: 连接(必要时拉起) core 并转发指令

模块规范:
    modules/<名字>/<名字>.py   唯一加载目标
    modules/<名字>/<名字>.json 市场元数据; core 读取 ID 用于同 role 替换
    目标文件导出 Module        固定为一个名为 Module 的类
    模块里直接使用 G / T       由 core 在 exec_module 之前注入模块全局命名空间

初始化:
    1. 读取 <名字>.json 的 ID
    2. 实例化全部 Module, 同 role 模块用 ID 时间戳大的替换旧的
    3. 依次调用 call()  一次性初始化, 允许调整 G.MODULES_LIST 顺序
    4. 阻塞监听并分发指令; call() 失败的模块会被移除
    used=False 的模块仍会被加载和初始化, 但不会出现在 sun help 中,
    也不能通过 sun <role> 调用

路由:
    sun                     确保 core 运行(不在则拉起, 在则直接返回)
    sun stop                停止 core
    sun help                core 内建帮助和模块列表
    sun <模块>               调用模块的 run()
    sun <模块> <子命令> ...  调用 subcommands 中登记的方法
    role 是主键, name 是可选别名; 查找时 role 优先, name 兜底
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

try:
    import fcntl
except ImportError:  # Windows
    fcntl = None

try:
    import msvcrt
except ImportError:  # Linux
    msvcrt = None

APP_PATH = Path(__file__).resolve().parent
# 保证模块可以用 from modules.<名字> import <辅助文件>
if str(APP_PATH) not in sys.path:
    sys.path.insert(0, str(APP_PATH))

MODULES_DIR = APP_PATH / "modules"
# core 自己的运行时文件放在 core/ 目录, 根目录保持简洁
CORE_DIR = APP_PATH / "core"
SOCKET_PATH = CORE_DIR / "core.sock"
LOCK_PATH = CORE_DIR / "core.lock"
PID_PATH = CORE_DIR / "core.pid"
CORE_LOG = CORE_DIR / "core.log"
# 模块共享日志的默认位置
DEFAULT_LOG_PATH = APP_PATH / "log" / "sun.log"

# 目标 py 里固定导出的类名
MODULE_EXPORT = "Module"
# core 内建命令, 不参与模块路由
RESERVED_COMMANDS = {"help", "stop"}
# 模块请求客户端确认时的退出码
CONFIRM_EXIT_CODE = 3
# G 自身占用的属性, 不允许模块名/role 覆盖
_G_RESERVED = {
    "APP_PATH", "MODULES_LIST", "by_name", "by_role", "_module_attrs", "get", "log",
}


class G:
    """全局共享对象: core 注入到每个模块的全局命名空间"""

    def __init__(self):
        self.APP_PATH = str(APP_PATH)
        self.MODULES_LIST = []
        self.by_name = {}
        self.by_role = {}
        self._module_attrs = set()

    def get(self, key, default=None):
        """按 role 或 name 取模块; role 是主键, name 是别名"""
        mod = self.by_role.get(key)
        if mod is None:
            mod = self.by_name.get(key)
        return default if mod is None else mod

    def log(self, message: str, path: str = "") -> None:
        """追加日志: 时间戳 + 内容; path 为空时写入 ./log/sun.log"""
        target = Path(path) if path else DEFAULT_LOG_PATH
        if not target.is_absolute():
            target = APP_PATH / target
        target.parent.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with open(target, "a", encoding="utf-8") as f:
            f.write(f"[{stamp}] {message}\n")

    def __contains__(self, key):
        return key in self.by_role or key in self.by_name


class T:
    """临时共享对象: 每次请求的上下文放在这里"""

    def __init__(self):
        self.CLIENT_CWD = str(APP_PATH)


class Sunday:
    def __init__(self):
        self.G = G()
        self.T = T()
        self._stop = False
        self._lock_fh = None
        self._sock = None

    # ------------------------------------------------------------------ #
    # 致命错误: 只处理 core 自身无法继续运行的情况
    # ------------------------------------------------------------------ #

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

    # ------------------------------------------------------------------ #
    # 模块加载
    # ------------------------------------------------------------------ #

    def load_modules(self):
        """扫描 modules/<名字>/<名字>.py 并实例化"""
        if not MODULES_DIR.is_dir():
            return

        for mod_dir in sorted(MODULES_DIR.iterdir()):
            if not mod_dir.is_dir() or mod_dir.name.startswith("_"):
                continue
            target = mod_dir / f"{mod_dir.name}.py"
            if target.is_file():
                self._load_one(mod_dir.name, target)

        self.rebuild_registry()

    def _load_one(self, dir_name: str, path: Path):
        # manifest 与模块 py 同名: modules/example/example.json
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
            return

        cls = getattr(module, MODULE_EXPORT, None)
        if not isinstance(cls, type):
            return
        try:
            inst = cls()
        except Exception:
            return

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

        if not self._normalize_instance(inst):
            return

        # 同 role 模块: ID 时间戳大的替换旧的
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

    def _pick(self, candidates, kind: str, token: str):
        """同一 token 多个候选: ID 时间戳大者优先, 相同则先加载的优先"""
        ordered = sorted(candidates, key=lambda item: (-self._module_timestamp(item[1]), item[0]))
        return ordered[0][1]

    def rebuild_registry(self):
        """重建 name/role 索引, 并把胜出模块挂到 G.<token>"""
        G = self.G

        for key in list(getattr(G, "_module_attrs", set())):
            if key in G.__dict__:
                try:
                    delattr(G, key)
                except Exception:
                    pass
        G._module_attrs = set()

        name_candidates = {}
        role_candidates = {}
        for order, mod in enumerate(G.MODULES_LIST):
            name = getattr(mod, "name", None)
            if name:
                name_candidates.setdefault(str(name), []).append((order, mod))

            role = getattr(mod, "role", None)
            if isinstance(role, str) and role:
                role_candidates.setdefault(role, []).append((order, mod))

        G.by_name = {
            token: self._pick(items, "name", token)
            for token, items in name_candidates.items()
        }
        G.by_role = {
            token: self._pick(items, "role", token)
            for token, items in role_candidates.items()
        }

        for token in set(G.by_name) | set(G.by_role):
            if token in _G_RESERVED or not token.isidentifier():
                continue
            if hasattr(G, token):
                continue
            mod = self.resolve(token)
            if mod is not None:
                setattr(G, token, mod)
                G._module_attrs.add(token)

    def resolve(self, token: str):
        """role 是主键, name 是别名"""
        mod = self.G.by_role.get(token)
        if mod is not None:
            return mod
        return self.G.by_name.get(token)

    # ------------------------------------------------------------------ #
    # 初始化
    # ------------------------------------------------------------------ #

    def run_call(self):
        """一次性初始化: 模块可在此调整 G.MODULES_LIST 顺序, 也可以追加新实例"""
        queue = list(self.G.MODULES_LIST)
        seen = set()
        while queue:
            mod = queue.pop(0)
            if id(mod) in seen:
                continue
            if mod not in self.G.MODULES_LIST:
                continue
            seen.add(id(mod))

            if not self._normalize_instance(mod):
                if mod in self.G.MODULES_LIST:
                    self.G.MODULES_LIST.remove(mod)
                continue

            fn = getattr(mod, "call", None)
            if callable(fn):
                try:
                    fn()
                except Exception:
                    if mod in self.G.MODULES_LIST:
                        self.G.MODULES_LIST.remove(mod)

            # call() 期间追加的新实例继续排队, 保证它们也会执行 call()
            for new_mod in self.G.MODULES_LIST:
                if id(new_mod) not in seen and new_mod not in queue:
                    queue.append(new_mod)

        self.rebuild_registry()

    # ------------------------------------------------------------------ #
    # 命令分发
    # ------------------------------------------------------------------ #

    def cmd_help(self):
        print("这里是 sunday: 基于 Python 的命令行工具框架")
        print("用法:")
        print("  sun                    确保 core 运行")
        print("  sun help               显示帮助和模块列表")
        print("  sun stop               停止 core")
        print("  sun <role>             调用模块的 run()")
        print("  sun <role> <子命令> ... 调用 subcommands 中登记的方法")
        print()
        print("可用模块:")
        visible = [m for m in self.G.MODULES_LIST if getattr(m, "_used", True)]
        if not visible:
            print("  (无)")
            return
        for mod in visible:
            self._print_module_help(mod)

    @staticmethod
    def _first_doc(fn) -> str:
        doc = getattr(fn, "__doc__", "") or ""
        doc = doc.strip()
        return doc.splitlines()[0] if doc else ""

    def _print_module_help(self, mod):
        role = getattr(mod, "role", "?")
        name = getattr(mod, "name", None)
        desc = getattr(mod, "desc", "")

        title = f"  {role}"
        if name and name != role:
            title += f"  (name: {name})"
        print(title)
        if desc:
            print(f"    {desc}")

        run_fn = getattr(mod, "run", None)
        if callable(run_fn):
            line = f"    默认: sun {role}"
            doc = self._first_doc(run_fn)
            if doc:
                line += f"  - {doc}"
            print(line)

        subs = getattr(mod, "subcommands", []) or []
        if subs:
            print("    子命令:")
            for sub in subs:
                line = f"      sun {role} {sub} ..."
                doc = self._first_doc(getattr(mod, sub, None))
                if doc:
                    line += f"  - {doc}"
                print(line)
        else:
            print("    子命令: (无)")

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
        if head == "stop":
            self._stop = True
            print("core 已停止")
            return

        mod = self.resolve(head)
        if mod is None:
            raise KeyError(f"未知指令: {head}")
        if not getattr(mod, "_used", True):
            raise AttributeError(f"模块 {head} 未开放给 sun")

        # 有子命令: 只允许 subcommands 中登记的方法
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

        # 无子命令: 调用默认 run()
        fn = getattr(mod, "run", None)
        if not callable(fn):
            raise AttributeError(f"模块 {head} 没有 run 方法")
        fn()

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

    # ------------------------------------------------------------------ #
    # 单实例锁
    # ------------------------------------------------------------------ #

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

    # ------------------------------------------------------------------ #
    # 常驻服务
    # ------------------------------------------------------------------ #

    def _install_signals(self):
        def _on_signal(signum, _frame):
            self._stop = True

        for sig in (signal.SIGINT, signal.SIGTERM):
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
            self.load_modules()
            self.run_call()

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


# ---------------------------------------------------------------------- #
# 客户端
# ---------------------------------------------------------------------- #

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
            answer = input("回车将继续，输入其他内容取消: ")
        except (EOFError, KeyboardInterrupt):
            print("\n[sun] 已取消", file=sys.stderr)
            return 1
        if answer.strip():
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
