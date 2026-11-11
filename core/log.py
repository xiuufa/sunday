"""core 工具: 日志写入"""

from datetime import datetime
from pathlib import Path

APP_PATH = Path(__file__).resolve().parents[1]
DEFAULT_LOG_PATH = APP_PATH / "log" / "sun.log"


def write(message: str, path: str = "") -> None:
    """追加一条带时间戳的日志; path 为空时写入 log/sun.log"""
    target = Path(path) if path else DEFAULT_LOG_PATH
    if not target.is_absolute():
        target = APP_PATH / target
    target.parent.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with open(target, "a", encoding="utf-8") as f:
        f.write(f"[{stamp}] {message}\n")
