"""core 工具: 模块初始化

init.json 是模块列表:
[
    {"name": "market", "used": true, "init": true},
    {"name": "dzbc", "used": true, "init": false}
]

初始化 = 挂载 G.<模块> = 实例 + 调用模块的 init()。
used=False 的模块不开放给 sun; used 和模块 JSON 的 init 都为 True 时, core 启动后自动初始化。
"""

import json
import traceback
from pathlib import Path

APP_PATH = Path(__file__).resolve().parents[1]
INIT_PATH = APP_PATH / "core" / "init.json"


class Initializer:
    def __init__(self, G, loader, reserved=()):
        self.G = G
        self._loader = loader
        self._reserved = set(reserved)
        self.done = set()
        self._mods = []

    def load(self) -> list:
        """读取 init.json, 统一成 [{"name", "used", "init"}]"""
        try:
            data = json.loads(INIT_PATH.read_text(encoding="utf-8"))
        except Exception:
            return []
        if isinstance(data, dict):
            data = data.get("mount") or data.get("modules") or []
        if not isinstance(data, list):
            return []

        entries = []
        for item in data:
            if isinstance(item, str):
                name = item.strip()
                if name:
                    entries.append({"name": name, "used": True, "init": True})
                continue
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or item.get("role") or "").strip()
            if not name:
                continue
            entries.append({
                "name": name,
                "used": bool(item.get("used", True)),
                "init": bool(item.get("init", False)),
            })
        return entries

    def save(self, entries: list) -> None:
        INIT_PATH.parent.mkdir(parents=True, exist_ok=True)
        data = [
            {
                "name": str(item.get("name") or "").strip(),
                "used": bool(item.get("used", True)),
                "init": bool(item.get("init", False)),
            }
            for item in entries
            if str(item.get("name") or "").strip()
        ]
        INIT_PATH.write_text(
            json.dumps(data, ensure_ascii=False, indent=4) + "\n",
            encoding="utf-8",
        )

    def policy(self, *tokens):
        """按 role/name 查 init.json 中的配置; 没有配置返回 None"""
        names = {str(token) for token in tokens if token}
        for item in self.load():
            if item["name"] in names:
                return item
        return None

    def ensure(self, token: str, used: bool = True, init: bool = True) -> bool:
        """把模块追加到 init.json; 已存在返回 False"""
        token = str(token or "").strip()
        if not token:
            return False
        entries = self.load()
        if any(item["name"] == token for item in entries):
            return False
        entries.append({"name": token, "used": used, "init": init})
        self.save(entries)
        return True

    def set(self, token: str, used=None, init=None) -> bool:
        """更新 init.json 中的 used/init; 条目不存在时追加"""
        token = str(token or "").strip()
        if not token:
            return False
        entries = self.load()
        for item in entries:
            if item["name"] != token:
                continue
            if used is not None:
                item["used"] = bool(used)
            if init is not None:
                item["init"] = bool(init)
            self.save(entries)
            return True
        entries.append({
            "name": token,
            "used": True if used is None else bool(used),
            "init": False if init is None else bool(init),
        })
        self.save(entries)
        return True

    def remove(self, token: str) -> bool:
        """从 init.json 移除模块; 不存在返回 False"""
        token = str(token or "").strip()
        entries = self.load()
        kept = [item for item in entries if item["name"] != token]
        if len(kept) == len(entries):
            return False
        self.save(kept)
        return True

    def is_initialized(self, *tokens) -> bool:
        return any(str(token) in self.done for token in tokens if token)

    def mount(self, mod) -> None:
        """挂载: G.<name> = 实例, G.<role> = 实例"""
        for token in (getattr(mod, "name", None), getattr(mod, "role", None)):
            if not token or not str(token).isidentifier() or token in self._reserved:
                continue
            setattr(self.G, token, mod)

    def run(self, names=None, quiet: bool = True) -> list:
        """初始化模块; names=None 时按 init.json 的 used && init 执行, 空列表表示不初始化"""
        if names is None:
            targets = [
                item["name"] for item in self.load()
                if item["used"] and item["init"]
            ]
        else:
            targets = [str(name) for name in names if str(name).strip()]

        loaded = []
        for token in targets:
            if self.is_initialized(token):
                if not quiet:
                    print(f"已初始化: {token}")
                continue
            mod = self._loader(token)
            if mod is None:
                self.G.log(f"初始化失败: 找不到模块 {token}")
                if not quiet:
                    print(f"未找到模块: {token}")
                continue

            self.mount(mod)
            fn = getattr(mod, "init", None)
            if callable(fn):
                try:
                    fn()
                except Exception:
                    self.G.log(
                        f"模块 {token} 初始化失败\n{traceback.format_exc()}"
                    )
                    if not quiet:
                        print(f"初始化失败: {token}")
                    continue

            self.done.update(filter(None, (
                str(token), getattr(mod, "name", ""), getattr(mod, "role", ""),
            )))
            self._mods.append(mod)
            loaded.append(mod)
            if not quiet:
                print(f"已初始化: {token}")
        return loaded

    def shutdown(self) -> None:
        """core 退出时按初始化逆序调用模块可选的 shutdown()"""
        for mod in reversed(self._mods):
            fn = getattr(mod, "shutdown", None)
            if not callable(fn):
                continue
            try:
                fn()
            except Exception:
                self.G.log(
                    f"模块 {getattr(mod, 'role', '?')} shutdown() 失败\n"
                    f"{traceback.format_exc()}"
                )
        self._mods.clear()
        self.done.clear()
