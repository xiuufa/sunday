import json
import os
from pathlib import Path

# 配置文件固定放在项目根的 data/config 下(不依赖 cwd, 保证 P0 与服务子进程读写同一份)
CONFIG_DIR = Path(__file__).resolve().parent.parent / "data" / "config"
CONFIG_PATH = CONFIG_DIR / "config.json"


class Config:
    def __init__(self, G):
        self.g = G
        self.name = "config"
        self.call_is_runnig: bool = False
        self.config_data: dict = {}
        self._called: bool = False

    @staticmethod
    def _defaults() -> dict:
        """默认配置项: 仅在缺失时补充, 不覆盖已有值"""
        root = Path(__file__).resolve().parent.parent
        return {
            "core_path": str(root),
            "download_dir": str(root / "download"),
            "emo_path": str(root / "data" / "emo"),
        }

    def call(self):
        """加载配置并完成初始化: 补默认项、同步 core_path, 有变化才落盘

        配置文件缺失或损坏时以空配置启动, 由默认项兜底
        """
        if self._called:
            return
        self._called = True
        changed = False
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                self.config_data = json.load(f)
            self.call_is_runnig = True
        except Exception as e:
            self.call_is_runnig = False
            self.config_data = {}
            self.g.log.run(f"{self.name} 加载配置失败：{str(e)}")
        # 补充缺失的默认项
        for key, value in self._defaults().items():
            if key not in self.config_data:
                self.config_data[key] = value
                changed = True
        # 程序所在目录变化时同步 core_path(原先由 sun_cli._sync_core_path 越权完成)
        core_path = str(Path(__file__).resolve().parent.parent)
        if self.config_data.get("core_path") != core_path:
            self.config_data["core_path"] = core_path
            changed = True
        if changed:
            self.save()

    def run(self, key: str):
        if key in self.config_data:
            return self.config_data[key]
        else:
            self.g.log.run(f"{self.name} 配置项 {key} 不存在")
            return None

    def get(self, key: str, default=None):
        """读取配置项, 不存在时返回 default"""
        return self.config_data.get(key, default)

    def set(self, key: str, value):
        """写入配置项并保存到 config.json"""
        self.config_data[key] = value
        self.save()

    def save(self):
        if not CONFIG_DIR.exists():
            os.makedirs(CONFIG_DIR, exist_ok=True)
            self.g.log.run(f"{self.name} data/config 目录缺失已重新创建")
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(self.config_data, f, ensure_ascii=False, indent=4)
