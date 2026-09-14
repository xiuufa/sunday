import fcntl
import os
from datetime import datetime


class Log:
    def __init__(self, G):
        # g 为全局对象引用, 通过 self.g.xxx 访问其他模块
        self.g = G
        self.call_is_runnig: bool = False

    def call(self):
        if self.call_is_runnig:
            return
        self.g.log.run("Log 启动成功")
        self.call_is_runnig = True

    def run(self, log: str, log_file_name: str = "log.txt"):
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        line = f"[{ts}] {log}\n"

        # 确保日志目录存在, 避免首次运行直接报错
        try:
            os.makedirs("log", exist_ok=True)
            with open(f"log/{log_file_name}", "a", encoding="utf-8") as f:
                # 使用文件 lock 避免多线程竞争 IO
                fcntl.flock(f.fileno(), fcntl.LOCK_EX)
                try:
                    f.write(line)
                    f.flush()
                    return True
                finally:
                    fcntl.flock(f.fileno(), fcntl.LOCK_UN)
        except Exception:
            return False
