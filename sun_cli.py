#!/usr/bin/env python3
"""
sun CLI 入口
用法:
    sun                 # P0: 唤醒所有应自启的后台服务(含 mgmg GUI), 自身随即退出
    sun list            # 列出所有可调用命令(按调用次数降序)
    sun <模块> [参数...]  # 调用 mudels 中对应模块的 run()

流程:
    1. 扫描 mudels/ 目录, 加载每个 .py 模块并实例化其中的类
    2. 按依赖顺序调用所有实例的 call() 完成初始化
    3. 无参数(P0 唯一唤醒者): 各模块 call() 注册自己的后台服务后, 由 cmd 按 config 开关统一唤醒, P0 随即退出
    4. 有参数: 第一个参数当作模块名, 调用其 run(剩余参数)
"""
import importlib.util
import inspect
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.resolve()
MODULES_DIR = PROJECT_ROOT / "mudels"
# 不需要被实例化的模块(基础类等)
SKIP_MODULES = {"base"}
# 初始化顺序(依赖在前的模块先 call; log/config 先行以便其他模块取用时已可用)
INIT_ORDER = ["log", "config", "cmd", "mgmg", "lobo"]
# 默认GUI模块
DEFAULT_GUI = "mgmg"

# 让模块间可以互相 import, 如 cmd.py 中的 from base import display_name
sys.path.insert(0, str(MODULES_DIR))
from base import display_name  # noqa: E402  (mudels 已在 sys.path 上, 需在 insert 之后导入)


class G:
    """全局对象: 模块实例/模块按文件名挂载, 各模块内通过 self.g 访问, 例如 self.g.log, self.g.config"""

    def __init__(self):
        self._modules = {}

    def __getattr__(self, name):
        try:
            return self._modules[name]
        except KeyError:
            raise AttributeError(f"'G' object has no attribute '{name}'")

    def __setattr__(self, name, value):
        if name == "_modules":
            super().__setattr__(name, value)
        else:
            self._modules[name] = value

    def __contains__(self, name):
        return name in self._modules


def load_module(g: G, name: str):
    """加载 mudels/<name>.py, 注入全局 G, 返回模块对象"""
    path = MODULES_DIR / f"{name}.py"
    if not path.exists():
        return None
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    module.G = g  # 模块内直接使用全局 G
    spec.loader.exec_module(module)
    return module


def instantiate(g: G, name: str, module):
    """实例化模块中第一个定义的类(构造参数为G); 无类的函数模块(如emo.py)直接挂载模块本身"""
    classes = [
        obj for _, obj in inspect.getmembers(module, inspect.isclass)
        if obj.__module__ == module.__name__
    ]
    if not classes:
        setattr(g, name, module)
        return None
    try:
        instance = classes[0](g)
    except TypeError:
        instance = classes[0]()
    setattr(g, name, instance)
    return instance


def _log(g: G, msg: str):
    """写一条运行日志: 优先写入日志模块, 同时回退打印到 stderr"""
    try:
        log = getattr(g, "log", None)
        if log is not None and callable(getattr(log, "run", None)):
            log.run(msg)
    except Exception:
        pass
    print(f"[sun] {msg}", file=sys.stderr)


def _log_fail(g: G, name: str, phase: str, exc: BaseException):
    """记录模块启动失败及其原因: 优先写入日志模块, 无可用日志时回退到 stderr 打印"""
    _log(g, f"模块 {name} {phase}: {exc}")


def load_all(g: G):
    """加载 mudels/ 下所有模块并实例化, 失败时记录原因"""
    for pyfile in sorted(MODULES_DIR.glob("*.py")):
        name = pyfile.stem
        if name.startswith("_") or name in SKIP_MODULES:
            continue
        try:
            module = load_module(g, name)
            if module is not None:
                instantiate(g, name, module)
        except Exception as e:
            _log_fail(g, name, "加载失败", e)


def call_module(g: G, name: str):
    """调用单个模块的 call() 初始化, 失败时记录原因"""
    inst = getattr(g, name, None)
    if inst is not None and callable(getattr(inst, "call", None)):
        try:
            inst.call()
        except Exception as e:
            _log_fail(g, name, "call() 初始化失败", e)


def run_calls(g: G, skip_mgmg: bool = False):
    """按依赖顺序调用所有模块的 call() 初始化, 失败时记录原因"""
    order = INIT_ORDER + [n for n in g._modules if n not in INIT_ORDER]
    for name in order:
        if skip_mgmg and name == DEFAULT_GUI:
            continue
        call_module(g, name)


def _record_usage(g: G, inst, fallback: str, method: str):
    """记录一次命令调用并写回 config(展示名与 sun list 用同一份解析, 失败不影响主流程)"""
    try:
        cmd = getattr(g, "cmd", None)
        if cmd is not None and callable(getattr(cmd, "record", None)):
            cmd.record(display_name(inst, fallback), method)
    except Exception:
        pass


def build_command_map(g: G):
    """构建 命令名 -> 模块实例 的映射; 模块的 name 支持字符串或别名列表"""
    cmd_map = {}
    for file_name, inst in list(g._modules.items()):
        names = getattr(inst, "name", None)
        keys = names if isinstance(names, list) else ([names] if names else [file_name])
        for key in keys:
            if key and key not in cmd_map:
                cmd_map[key] = inst
    return cmd_map


def print_help():
    print("用法: sun [模块] [子命令] [参数...]")
    print("  无参数: 打开默认 GUI (mgmg)")
    print("  sun --auto    开机自启入口(检查配置开关后决定是否打开GUI)")
    print("  sun list      列出所有可调用命令(按调用次数降序)")
    print("  例: sun list         → 列出所有可调用命令")
    print("  例: sun mgmg stop    → 关闭GUI并禁止下次自动启动 (run_methods 子命令)")
    print("  例: sun mgmg list    → 列出 mgmg 的可调用子命令")
    print("可用模块:")
    for pyfile in sorted(MODULES_DIR.glob("*.py")):
        name = pyfile.stem
        if not name.startswith("_") and name not in SKIP_MODULES:
            print(f"  {name}")


def enable_autostart(g: G, enabled: bool):
    """配置开机自启(平台分叉): Linux 写 ~/.config/autostart/sun.desktop, Windows 写启动文件夹脚本

    绿色软件惯例: 自启是可选动作, 由用户显式调用, 不随安装脚本做
    """
    try:
        if sys.platform.startswith("win"):
            _set_autostart_windows(enabled)
        else:
            _set_autostart_linux(enabled)
    except Exception as e:
        print(f"[sun] 设置开机自启失败: {e}", file=sys.stderr)
        sys.exit(1)


def _set_autostart_linux(enabled: bool):
    """Linux: 创建/删除 ~/.config/autostart/sun.desktop(登录后经 sun --auto 拉起桌宠)"""
    desktop_dir = Path(os.path.expanduser("~/.config/autostart"))
    desktop_file = desktop_dir / "sun.desktop"
    if enabled:
        desktop_dir.mkdir(parents=True, exist_ok=True)
        content = (
            "[Desktop Entry]\n"
            "Type=Application\n"
            "Name=sun 桌宠\n"
            "Comment=开机自动启动 sunday 桌宠\n"
            f"Exec=\"{PROJECT_ROOT / 'sun'}\" --auto\n"
            "Terminal=false\n"
            "X-GNOME-Autostart-enabled=true\n"
        )
        desktop_file.write_text(content, encoding="utf-8")
        print(f"[sun] 已开启开机自启: {desktop_file}")
    else:
        if desktop_file.exists():
            desktop_file.unlink()
            print(f"[sun] 已关闭开机自启: {desktop_file}")
        else:
            print("[sun] 未找到开机自启配置, 无需关闭")


def _set_autostart_windows(enabled: bool):
    """Windows: 创建/删除启动文件夹里的静默启动脚本(登录后经 sun.bat --auto 拉起桌宠)

    用 %APPDATA%\\...\\Startup\\sun_autostart.vbs 而非注册表: 与 Linux 的 .desktop 同形态, 删文件即卸载
    """
    startup_dir = (
        Path(os.environ.get("APPDATA", ""))
        / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"
    )
    vbs_file = startup_dir / "sun_autostart.vbs"
    if enabled:
        startup_dir.mkdir(parents=True, exist_ok=True)
        # VBS 以隐藏窗口方式拉起 sun.bat(避免每次开机闪黑窗), 0=隐藏
        content = (
            'Set WshShell = CreateObject("WScript.Shell")\n'
            f'WshShell.Run """{PROJECT_ROOT / "sun.bat"}"" --auto", 0, False\n'
        )
        vbs_file.write_text(content, encoding="utf-8")
        print(f"[sun] 已开启开机自启: {vbs_file}")
    else:
        if vbs_file.exists():
            vbs_file.unlink()
            print(f"[sun] 已关闭开机自启: {vbs_file}")
        else:
            print("[sun] 未找到开机自启配置, 无需关闭")


def main():
    args = sys.argv[1:]
    # 开机自启标记: sun --auto 与无参数等价(各服务是否唤醒由 config 的 <服务名>_auto_start 决定)
    args = [a for a in args if a != "--auto"]

    # 绿色软件的自启管理子命令: 与模块调用独立, 不加载任何模块, 轻量执行
    if args and args[0] == "--autostart":
        if len(args) < 2 or args[1] not in ("on", "off"):
            print("用法: sun --autostart on|off", file=sys.stderr)
            sys.exit(1)
        enable_autostart(None, args[1] == "on")
        return

    cmd = args[0] if args else None
    # GUI 路径(默认 sun 或 sun mgmg run): 由 cmd 拉起独立后台服务, 父进程不进入主循环
    # 注: SUN_DAEMON 标识当前进程就是那个后台 GUI 服务子进程(由 cmd 注入)
    gui_path = (not args) or (cmd == DEFAULT_GUI and args[1:] == ["run"])
    is_service = bool(os.environ.get("SUN_DAEMON"))
    # 未知模块的 --xxx 开头参数(除 --auto/--autostart 外)视为非法用法, 避免被当成模块名
    if args and cmd and cmd.startswith("-"):
        print(f"[sun] 错误: 未知选项 '{cmd}'", file=sys.stderr)
        print_help()
        sys.exit(1)

    g = G()
    load_all(g)
    # 所有模块实例已挂载, 各模块在 call() 里用到谁的状态就现取, 因此不需要额外的前置初始化

    if gui_path and not is_service:
        # P0(唯一唤醒者): 加载模块让各服务完成注册, 再按 config 开关统一唤醒需要的服务, 自身立即退出
        run_calls(g, skip_mgmg=False)
        cmd_mod = getattr(g, "cmd", None)
        started = cmd_mod.autostart_services() if cmd_mod is not None else []
        if started:
            print(f"[sun] 已唤醒后台服务: {', '.join(started)}")
        else:
            print("[sun] 后台服务均已在运行或已按配置关闭")
        return

    # CLI 命令模式不拉起 GUI; 但 sun mgmg run / GUI 服务子进程需要初始化 GUI
    skip_mgmg = bool(args) and not (cmd == DEFAULT_GUI and args[1:] == ["run"])
    run_calls(g, skip_mgmg=skip_mgmg)

    # 无参数(仅服务子进程): 直接进入 mgmg 主循环
    if not args:
        mgmg = getattr(g, DEFAULT_GUI, None)
        if mgmg is None or not hasattr(mgmg, "run"):
            print(f"[sun] 错误: 默认 GUI 模块 {DEFAULT_GUI} 不可用", file=sys.stderr)
            sys.exit(1)
        mgmg.run()
        return

    inst = build_command_map(g).get(cmd)
    if inst is None:
        print(f"[sun] 错误: 未知模块 '{cmd}'", file=sys.stderr)
        print_help()
        sys.exit(1)

    # 子命令: 模块通过 run_methods 声明可调用的方法列表, 如 sun mgmg stop
    if len(args) > 1:
        sub, sub_args = args[1], args[2:]
        # 内置命令: sun <模块> list → 列出该模块可调用的子命令(走 cmd 模块)
        if sub == "list":
            cmd_mod = getattr(g, "cmd", None)
            if cmd_mod is not None and callable(getattr(cmd_mod, "list", None)):
                cmd_mod.list(inst)
            else:
                print_help()
            return
        methods = getattr(inst, "run_methods", ["run"])
        if sub in methods and callable(getattr(inst, sub, None)):
            try:
                getattr(inst, sub)(*sub_args)
                # 统计: 记录子命令调用
                _record_usage(g, inst, cmd, sub)
            except TypeError as e:
                print(f"[sun] 调用 {cmd} {sub} 时参数错误: {e}", file=sys.stderr)
                sys.exit(1)
            return

    # 未声明 run_methods 的模块沿用旧方式: sun <模块> <参数...> → run(...)
    if not hasattr(inst, "run"):
        print(f"[sun] 错误: 模块 '{cmd}' 没有 run 方法", file=sys.stderr)
        sys.exit(1)
    try:
        inst.run(*args[1:]) if args[1:] else inst.run("")
        # 统计: 记录 run 调用
        _record_usage(g, inst, cmd, "run")
    except TypeError as e:
        print(f"[sun] 调用 {cmd} 时参数错误: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
