"""lobo: 萝卜快传 —— 局域网共享传输板（sun 插件）

一台设备运行本模块（主机），其它手机/电脑扫码或打开网址即可加入同一块传输板；
不区分主机/客户端，两端都可以启动服务。

CLI:
    sun lobo                 启动服务并打开网页（已在运行则只打开网页）
    sun lobo run             同 sun lobo
    sun lobo get             打印最近一次收到的文件地址；若是文字则写入粘贴板
    sun lobo send [路径]     无参数时发送粘贴板文字，否则发送指定文件
    sun lobo stop            停止 HTTP 服务
    sun lobo status          查看运行状态与地址
    sun lobo help            帮助

配置: 同目录 lobo.json（port / save_dir / host / binary / open_browser / sender）。
服务由 core 托管（G.server），实际运行 modules/lobo/bin/ 下的萝卜快传单文件程序，
网页与 HTTP API 与安卓/桌面端完全一致。
"""

import json
import mimetypes
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from modules.lobo import clipboard

SERVICE_NAME = "lobo"


def _reap_child(pid):
    """回收被 G.server 停掉的子进程, 避免 core 里留下僵尸进程"""
    if not pid or os.name == "nt":
        return
    for _ in range(10):
        try:
            done, _ = os.waitpid(pid, os.WNOHANG)
        except ChildProcessError:
            return
        except OSError:
            return
        if done == pid:
            return
        time.sleep(0.05)
DEFAULT_PORT = 8080
DEFAULT_CHUNK = 1024 * 1024

MODULE_DIR = Path(__file__).resolve().parent
APP_PATH = MODULE_DIR.parents[1]
CONFIG_PATH = MODULE_DIR / "lobo.json"
# 记录“网页已经打开过”的标记: 服务存活期间重复执行 sun lobo 不再新开标签
PAGE_MARKER = APP_PATH / "data" / "services" / "lobo.page"

DEFAULT_CONFIG = {
    "port": DEFAULT_PORT,
    "save_dir": "data/lobo",
    "host": "",
    "binary": "",
    "open_browser": True,
    "sender": "sun",
}


class LoboError(RuntimeError):
    pass


def _load_config() -> dict:
    cfg = dict(DEFAULT_CONFIG)
    try:
        raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return cfg
    except Exception as exc:
        print(f"lobo.json 解析失败，使用默认配置: {exc}")
        return cfg
    if isinstance(raw, dict):
        for key in cfg:
            if key in raw:
                cfg[key] = raw[key]
    return cfg


def _resolve(value, base: Path) -> Path:
    path = Path(os.path.expanduser(str(value)))
    return path if path.is_absolute() else (base / path)


def _default_binary() -> Path:
    name = "lobo-windows.exe" if os.name == "nt" else "lobo-linux-x86_64"
    return MODULE_DIR / "bin" / name


class Module:
    role = "lobo"
    name = "lobo"
    desc = "萝卜快传: 局域网共享传输板, 启动网页服务并收发文件/文字"
    subcommands = ["run", "get", "send", "stop", "status", "help"]

    def __init__(self):
        cfg = _load_config()
        try:
            self.port = int(cfg.get("port") or DEFAULT_PORT)
        except (TypeError, ValueError):
            self.port = DEFAULT_PORT
        self.save_dir = _resolve(cfg.get("save_dir") or "data/lobo", APP_PATH)
        self.host = str(cfg.get("host") or "").strip()
        self.sender = str(cfg.get("sender") or "sun").strip() or "sun"
        self.open_browser = bool(cfg.get("open_browser", True))
        binary = str(cfg.get("binary") or "").strip()
        self.binary = _resolve(binary, APP_PATH) if binary else _default_binary()
        self._binary_error = ""
        self._register()

    def _register(self):
        """挂载时登记后台服务, 不启动"""
        try:
            if not self.binary.is_file():
                self._binary_error = (
                    f"找不到萝卜快传可执行文件: {self.binary}\n"
                    "请把静态程序放到 modules/lobo/bin/，或修改 lobo.json 的 binary 字段"
                )
                return
            if os.name != "nt":
                try:
                    os.chmod(self.binary, 0o755)
                except OSError:
                    pass
            argv = [str(self.binary), "-p", str(self.port),
                    "-d", str(self.save_dir), "--no-browser"]
            if self.host:
                argv += ["--host", self.host]
            G.server.register(SERVICE_NAME, argv, marker=self.binary.name)
        except Exception as exc:
            self._binary_error = f"lobo 挂载失败: {exc}"

    # ---------------------------------------------------------------- 生命周期

    def init(self):
        """core 初始化时直接启动服务, 并向 GUI 注册快捷按钮"""
        self.run()
        self._register_buttons()

    def _register_buttons(self):
        """向 GUI 工具面板写入快捷按钮; 失败只记录日志"""
        for text, command in (("获取文本", "lobo get"), ("发送粘贴板", "lobo send")):
            try:
                gui = G.get("gui")
                if gui is None or not hasattr(gui, "add_button"):
                    raise RuntimeError("gui 模块不可用")
                if not gui.add_button(text, command):
                    raise RuntimeError("gui 未运行")
            except Exception as exc:
                G.log(f"lobo 注册按钮「{text}」失败: {exc}")

    # ---------------------------------------------------------------- CLI

    def run(self):
        """sun lobo: 启动服务并打开网页（已在运行则只打开网页）"""
        info = self._info()
        if info:
            print("lobo 已在运行，只打开网页")
            self._print_endpoints(info)
            self._open_page(info)
            return
        try:
            info = self._ensure_running()
        except LoboError as exc:
            print(exc)
            return
        print("lobo 已启动")
        self._print_endpoints(info)
        self._open_page(info)

    def get(self):
        """sun lobo get: 打印最近一次收到的文件地址；文字则写入粘贴板"""
        try:
            info = self._ensure_running()
            data = self._request(self._port(info), "GET", "/api/list?since=0")
        except LoboError as exc:
            print(exc)
            return
        except Exception as exc:
            print(f"读取传输板失败: {exc}")
            return

        own = info.get("deviceId")
        items = [it for it in (data.get("items") or []) if it.get("senderId") != own]
        if not items:
            print("还没有收到内容")
            return

        latest = max(items, key=lambda it: int(it.get("createdAt") or 0))
        if latest.get("kind") == "text":
            ok, detail = clipboard.copy_text(latest.get("text") or "")
            if ok:
                print("文字已写入粘贴板")
            else:
                print(f"文字已收到，但写入粘贴板失败: {detail}")
                print(latest.get("text") or "")
            return

        name = latest.get("fileName") or latest.get("name") or ""
        path = Path(info.get("storagePath") or self.save_dir) / name
        state = latest.get("state")
        if state == "receiving":
            print(f"最近一次收到的文件还没接收完: {path}")
            return
        if state == "failed":
            print(f"最近一次收到的文件已失效（主机上文件被删除）: {path}")
            return
        print(str(path))

    def send(self, *args):
        """sun lobo send [文件路径]: 无参数发送粘贴板文字, 有参数发送文件"""
        try:
            info = self._ensure_running()
        except LoboError as exc:
            print(exc)
            return
        port = self._port(info)
        sender = self.sender
        sender_id = info.get("deviceId") or ""

        target = " ".join(str(a) for a in args).strip()
        if not target:
            ok, text = clipboard.paste_text()
            if not ok:
                print(f"读取粘贴板失败: {text}")
                return
            if not text.strip():
                print("粘贴板为空，没有发送")
                return
            max_text = int(info.get("maxTextBytes") or 1048576)
            if len(text.encode("utf-8")) > max_text:
                print("粘贴板文字超过大小上限，没有发送")
                return
            try:
                self._request(port, "POST", "/api/text",
                              {"text": text, "sender": sender, "senderId": sender_id})
            except Exception as exc:
                print(f"发送失败: {exc}")
                return
            print(f"已发送粘贴板文字（{len(text)} 字符）")
            return

        path = _resolve(target, Path(getattr(T, "CLIENT_CWD", "") or APP_PATH))
        if not path.exists():
            print(f"文件不存在: {path}")
            return
        if path.is_dir():
            print(f"暂不支持发送文件夹: {path}")
            return
        size = path.stat().st_size
        max_upload = int(info.get("maxUploadBytes") or 0)
        if max_upload and size > max_upload:
            print(f"文件超过单文件上限（{max_upload} B）: {path}")
            return

        mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        try:
            init = self._request(port, "POST", "/api/upload/init",
                                 {"name": path.name, "size": size, "mime": mime,
                                  "sender": sender, "senderId": sender_id})
        except Exception as exc:
            print(f"发送失败: {exc}")
            return

        upload_id = init.get("uploadId") or init.get("itemId")
        if not upload_id:
            print("发送失败: 服务端响应缺少 uploadId")
            return
        offset = int(init.get("offset") or 0)
        chunk = int(info.get("chunkSize") or DEFAULT_CHUNK)
        try:
            with open(path, "rb") as fh:
                while offset < size:
                    fh.seek(offset)
                    blob = fh.read(chunk)
                    if not blob:
                        break
                    self._request(port, "PUT",
                                  f"/api/upload/{upload_id}?offset={offset}", raw=blob)
                    offset += len(blob)
            self._request(port, "POST", f"/api/upload/{upload_id}/complete")
        except Exception as exc:
            print(f"发送失败: {exc}")
            return
        print(f"已发送文件: {path}（{size} B）")

    def stop(self):
        """sun lobo stop: 停止 HTTP 服务"""
        info = self._info()
        stopped = False
        if info:
            try:
                self._request(self._port(info), "POST", "/api/shutdown", {}, timeout=3)
                stopped = True
            except Exception:
                pass
        try:
            status = G.server.status(SERVICE_NAME)
            if status["running"]:
                pid = status.get("pid")
                G.server.stop(SERVICE_NAME)
                _reap_child(pid)
                stopped = True
        except Exception:
            pass
        clipboard.stop_clipboard_daemon()
        try:
            PAGE_MARKER.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            pass
        print("lobo 已停止" if stopped else "lobo 未运行")

    def status(self):
        """sun lobo status: 运行状态与访问地址"""
        info = self._info()
        if not info:
            print("lobo: 未运行")
            return
        st = G.server.status(SERVICE_NAME)
        pid = f" (pid={st['pid']})" if st.get("pid") else ""
        print(f"lobo: 运行中{pid}")
        self._print_endpoints(info)
        print(f"传输板条目: {info.get('itemCount', 0)}")

    def help(self):
        """sun lobo help: 帮助"""
        print(self.desc)
        print("用法:")
        print("  sun lobo                启动服务并打开网页（已在运行则只打开网页）")
        print("  sun lobo run            同 sun lobo")
        print("  sun lobo get            打印最近一次收到的文件地址；若是文字则写入粘贴板")
        print("  sun lobo send [文件路径] 无参数发送粘贴板文字，否则发送该文件")
        print("  sun lobo stop           停止 HTTP 服务")
        print("  sun lobo status         运行状态与访问地址")
        print(f"保存位置: {self.save_dir}")
        print(f"配置文件: {CONFIG_PATH}（port / save_dir / host / binary / open_browser / sender）")
        print("说明: 任意一端都可以运行本服务；另一端扫码或打开网址即可加入传输板")

    # ---------------------------------------------------------------- 内部工具

    def _port(self, info=None) -> int:
        if info:
            try:
                return int(info.get("port") or self.port)
            except (TypeError, ValueError):
                return self.port
        return self.port

    def _info(self, timeout=1.5):
        """探测正在运行的服务（端口可能因占用而自动回退，以 .luobo.port 为准）"""
        ports = []
        try:
            raw = (self.save_dir / ".luobo.port").read_text(encoding="utf-8").strip()
            if raw.isdigit():
                ports.append(int(raw))
        except Exception:
            pass
        ports.append(self.port)
        tried = set()
        for port in ports:
            if port in tried:
                continue
            tried.add(port)
            try:
                return self._request(port, "GET", "/api/info", timeout=timeout)
            except Exception:
                continue
        return None

    def _ensure_running(self) -> dict:
        """确保服务在运行, 返回 /api/info"""
        if self._binary_error:
            raise LoboError(self._binary_error)
        if not self.binary.is_file():
            raise LoboError(f"找不到萝卜快传可执行文件: {self.binary}")
        info = self._info()
        if info:
            return info

        state = G.server.status(SERVICE_NAME)
        if not state["registered"]:
            self.init()
            if self._binary_error:
                raise LoboError(self._binary_error)
            state = G.server.status(SERVICE_NAME)
        if not state["running"]:
            try:
                (self.save_dir / ".luobo.port").unlink()
            except OSError:
                pass
            G.server.start(SERVICE_NAME)

        G.server.wait(SERVICE_NAME, timeout=10,
                        probe=lambda: self._info(timeout=1) is not None)
        info = self._info()
        if info:
            return info
        raise LoboError("lobo 服务启动失败\n" + self._log_tail())

    def _request(self, port, method, path, body=None, timeout=8, raw=None):
        url = f"http://127.0.0.1:{port}{path}"
        data = None
        headers = {}
        if raw is not None:
            data = raw
            headers["Content-Type"] = "application/octet-stream"
        elif body is not None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                payload = resp.read()
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", "replace")
            except Exception:
                pass
            raise LoboError(f"HTTP {exc.code}: {detail or exc.reason}") from None
        if not payload:
            return {}
        return json.loads(payload.decode("utf-8"))

    def _open_page(self, info):
        url = f"http://127.0.0.1:{self._port(info)}/"
        if not self.open_browser:
            print(f"网页地址: {url}")
            return
        # 服务存活期间只保留一个网页: 已经打开过就不再新开标签页
        try:
            if PAGE_MARKER.is_file() and PAGE_MARKER.read_text(encoding="utf-8").strip() == url:
                print(f"网页已在浏览器中打开: {url}")
                return
        except OSError:
            pass
        try:
            PAGE_MARKER.parent.mkdir(parents=True, exist_ok=True)
            PAGE_MARKER.write_text(url, encoding="utf-8")
        except OSError:
            pass
        if os.name == "nt":
            cmd = ["cmd", "/c", "start", "", url]
        elif sys.platform == "darwin":
            cmd = ["open", url]
        else:
            cmd = ["xdg-open", url]
        try:
            subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             start_new_session=os.name != "nt")
        except Exception as exc:
            print(f"请手动打开网页: {url}（{exc}）")

    def _print_endpoints(self, info):
        port = self._port(info)
        print(f"本机地址: http://127.0.0.1:{port}/")
        for url in info.get("urls") or []:
            if "127.0.0.1" not in url and url.rstrip("/") != f"http://127.0.0.1:{port}":
                print(f"局域网地址: {url}")
        print(f"保存位置: {info.get('storagePath') or self.save_dir}")

    def _log_tail(self, lines=15) -> str:
        log = APP_PATH / "core" / "services" / f"{SERVICE_NAME}.log"
        try:
            text = log.read_text(encoding="utf-8", errors="replace").splitlines()
        except Exception:
            return f"（无法读取服务日志 {log}）"
        return "\n".join(text[-lines:])
