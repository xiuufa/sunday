"""whiteblob 的共享常量: facade 与 serve 进程都会用到, 不依赖 core 注入的 G/T。"""

from pathlib import Path

APP_PATH = Path(__file__).resolve().parents[2]
SERVICE_NAME = "gui"
SOCKET_NAME = "gui.sock"


def sock_path() -> Path:
    return APP_PATH / "data" / "services" / SOCKET_NAME
