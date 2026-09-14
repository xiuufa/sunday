#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
lobo —— 萝卜服务模块
=====================
与手机 APP 通信(TCP 18989 / UDP 18990)，集成 pc_server + file_service + clipboard_service。

进程模型:
    - 服务以独立守护进程运行: sun lobo serve (由 cmd 统一登记/启停, P0 按 config 决定是否唤醒)
    - 桌宠进程内的本模块是"本地客户端", 通过 127.0.0.1:18989 与守护进程通信
    - 收到 APP 文本 → 调用剪贴板模块生成"〇"按钮; 文件收发进度 → 工具面板进度气泡

约定:
    - call(): 只注册服务(定义), 不解析下载目录; 是否自启由 P0 读 config 的 lobo_auto_start 决定
    - run():  sun lobo run 启动服务 / sun lobo serve 前台守护 / sun lobo <文件> 发送文件
    - stop(): sun lobo stop 停止服务
依赖: 仅 Python 标准库，无第三方依赖。
"""

import json
import logging
import math
import os
import shutil
import signal
import socket
import struct
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path

# --------------------------------------------------------------------------- #
# 日志
# --------------------------------------------------------------------------- #
_log = logging.getLogger("lobo")
_log.setLevel(logging.INFO)
if not _log.handlers:
    h = logging.StreamHandler()
    h.setFormatter(logging.Formatter("[%(asctime)s][%(levelname)s] %(message)s", datefmt="%H:%M:%S"))
    _log.addHandler(h)

# --------------------------------------------------------------------------- #
# 常量（与 Android 端 Protocol.kt 一致）
# --------------------------------------------------------------------------- #
DEFAULT_TCP_PORT = 18989
DEFAULT_UDP_PORT = 18990
DISCOVER_MAGIC = b"BAILUOBO_DISCOVER"
ACK_TIMEOUT_SEC = 1.0
SEND_CHUNK = 512 * 1024
RECV_BUFSIZE = 512 * 1024
FILE_CHUNK_SIZE = 512 * 1024
SOCKET_BUF = 1024 * 1024
LARGE_PAYLOAD_THRESHOLD = 16 * 1024 * 1024
FILE_ACK_DEADLINE_SEC = 10.0

TYPE_TEXT = "text"
TYPE_IMG = "img"
TYPE_AI_STREAM = "ai_stream"
TYPE_TOOL = "tool"
TYPE_ACK = "ack"
TYPE_CLIPBOARD = "clipboard"
TYPE_FILE_CHUNK = "file_chunk"
TYPE_FILE_CTRL = "file_ctrl"
# 本机客户端(桌宠/CLI) <-> 守护进程 专用类型(仅本机回环, 不发给 APP)
TYPE_LOCAL = "local"

LOCAL_HOST = "127.0.0.1"

# 在 cmd 中的服务注册名(与模块名一致, 同时也是 config 自启开关 lobo_auto_start 的前缀)
SERVICE_NAME = "lobo"

# --------------------------------------------------------------------------- #
# 输出目录
# --------------------------------------------------------------------------- #
# 默认相对路径，优先使用 config 中的绝对路径，失效或失败时退回到此路径
DEFAULT_DOWNLOAD_DIR = "download"


def _sun_log(msg: str):
    """写运行日志: 优先走全局 log 模块(log/log.txt), 不可用时回退到模块 logging"""
    try:
        log = getattr(G, "log", None)
        if log is not None and callable(getattr(log, "run", None)):
            log.run(msg)
            return
    except Exception:
        pass
    _log.info(msg)


def _output_dir() -> Path:
    """下载目录: 每次使用时从 config 现取并确保存在(不在模块里缓存, 避免依赖 config 的初始化顺序)

    config 里的绝对路径失效或不可写时, 退化为项目下的默认相对路径并记一条日志
    """
    fallback = Path(__file__).resolve().parent.parent / DEFAULT_DOWNLOAD_DIR
    config_path = None
    try:
        config = getattr(G, "config", None)
        if config is not None:
            config_path = config.get("download_dir")
            if config_path and os.path.isabs(config_path):
                output = Path(config_path)
                try:
                    output.mkdir(parents=True, exist_ok=True)
                    return output
                except OSError:
                    pass  # 无效路径, 回退到默认值
    except Exception:
        pass

    # 退化时记录原因, 便于排查配置里的绝对路径为何失效
    _sun_log(f"config download_dir 不可用({config_path!r}), 已退化为默认路径: {fallback}")
    try:
        fallback.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    return fallback


# --------------------------------------------------------------------------- #
# Frame
# --------------------------------------------------------------------------- #
class _Frame:
    __slots__ = ("msg_id", "type", "timestamp", "data", "payload_size",
                 "payload", "payload_path", "owns_payload")

    def __init__(self, msg_id, msg_type, timestamp, data="", payload=b"", payload_size=0):
        self.msg_id = msg_id
        self.type = msg_type
        self.timestamp = timestamp
        self.data = data if data else ""
        self.payload = payload if payload is not None else None
        self.payload_size = len(payload) if payload is not None else payload_size
        self.payload_path = None
        self.owns_payload = True

    @staticmethod
    def new(msg_type, data="", payload=b""):
        return _Frame(uuid.uuid4().hex, msg_type, int(time.time() * 1000), data, payload)

    def to_bytes(self):
        header = {
            "msg_id": self.msg_id, "type": self.type, "timestamp": self.timestamp,
            "data": self.data, "payload_size": self.payload_size,
        }
        hb = json.dumps(header, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        return struct.pack(">I", len(hb)) + hb + (self.payload or b"")

    @staticmethod
    def from_parts(header, payload):
        return _Frame(
            header.get("msg_id") or uuid.uuid4().hex,
            header.get("type", ""),
            header.get("timestamp", 0),
            header.get("data", ""),
            payload,
        )

    def read_payload(self):
        if self.payload is not None:
            return self.payload
        with open(self.payload_path, "rb") as f:
            return f.read()

    def cleanup_payload(self):
        if self.owns_payload and self.payload_path and os.path.exists(self.payload_path):
            try:
                os.remove(self.payload_path)
            except OSError:
                pass


# --------------------------------------------------------------------------- #
# FrameReader
# --------------------------------------------------------------------------- #
class _FrameReader:
    def __init__(self, stream):
        self.stream = stream
        self._buf = bytearray()

    def _fill(self, n):
        while len(self._buf) < n:
            chunk = self.stream.recv(RECV_BUFSIZE)
            if not chunk:
                raise EOFError("连接已关闭")
            self._buf.extend(chunk)

    def _read_exact(self, n):
        self._fill(n)
        out = bytes(self._buf[:n])
        del self._buf[:n]
        return out

    def next_frame(self, large_dir=None):
        lenb = self._read_exact(4)
        header_len = struct.unpack(">I", lenb)[0]
        if header_len <= 0 or header_len > 4 * 1024 * 1024:
            raise ValueError(f"非法 JSON 头长度: {header_len}")
        hb = self._read_exact(header_len)
        try:
            header = json.loads(hb.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as e:
            raise ValueError(f"JSON 头解析失败: {e}") from e

        payload_size = int(header.get("payload_size", 0))
        if payload_size == 0:
            return _Frame.from_parts(header, b"")
        if payload_size <= LARGE_PAYLOAD_THRESHOLD:
            return _Frame.from_parts(header, self._read_exact(payload_size))

        # 大文件流式写入临时文件
        fd, path = tempfile.mkstemp(prefix="lobo_", suffix=".bin", dir=large_dir)
        try:
            got = 0
            with os.fdopen(fd, "wb") as f:
                while got < payload_size:
                    want = min(RECV_BUFSIZE, payload_size - got)
                    self._fill(want)
                    take = min(want, len(self._buf))
                    f.write(self._buf[:take])
                    del self._buf[:take]
                    got += take
            frame = _Frame.from_parts(header, None)
            frame.payload_path = path
            return frame
        except Exception:
            try:
                os.remove(path)
            except OSError:
                pass
            raise


# --------------------------------------------------------------------------- #
# AckTracker
# --------------------------------------------------------------------------- #
class _AckTracker:
    def __init__(self, on_timeout=None):
        self._pending = {}
        self._lock = threading.Lock()
        self._on_timeout = on_timeout

    def register(self, msg_id):
        with self._lock:
            self._pending[msg_id] = time.monotonic()

    def resolve(self, msg_id):
        with self._lock:
            return self._pending.pop(msg_id, None) is not None

    def sweep(self):
        now = time.monotonic()
        timed_out = []
        with self._lock:
            for mid, t0 in list(self._pending.items()):
                if now - t0 > ACK_TIMEOUT_SEC:
                    timed_out.append(mid)
                    del self._pending[mid]
        for mid in timed_out:
            if self._on_timeout:
                self._on_timeout(mid)

    def start_monitor(self, stop_event, interval=0.2):
        def loop():
            while not stop_event.is_set():
                self.sweep()
                stop_event.wait(interval)
        threading.Thread(target=loop, daemon=True, name="ack-monitor").start()


# --------------------------------------------------------------------------- #
# OutgoingFileTransfer
# --------------------------------------------------------------------------- #
class _OutgoingFileTransfer:
    def __init__(self, server, file_id, path, kind=TYPE_TOOL,
                 progress=None, on_done=None, on_failed=None):
        self.server = server
        self.file_id = file_id
        self.path = path
        self.kind = kind
        self.total = os.path.getsize(path)
        self.total_chunks = max(1, math.ceil(self.total / FILE_CHUNK_SIZE))
        self.progress = progress or (lambda fid, sent, total: None)
        self.on_done = on_done or (lambda fid: None)
        self.on_failed = on_failed
        self.state = "sending"
        self.chunk_ids = set()
        self.sent_chunks = 0
        self.acked_chunks = 0
        self._pause_evt = threading.Event()
        self._cancel_evt = threading.Event()
        self._thread = None
        self._finish_lock = threading.Lock()  # 保证 _finish 只生效一次(ack 线程与发送线程可能并发)

    def run(self):
        name = os.path.basename(self.path) or "file"
        try:
            with open(self.path, "rb") as f:
                seq = 0
                while seq < self.total_chunks:
                    if self._cancel_evt.is_set():
                        self._finish("cancelled")
                        return
                    while self._pause_evt.is_set() and not self._cancel_evt.is_set():
                        time.sleep(0.1)
                    chunk = f.read(FILE_CHUNK_SIZE)
                    if not chunk:
                        break
                    meta = {
                        "file_id": self.file_id, "seq": seq, "total": self.total_chunks,
                        "name": name, "size": self.total, "kind": self.kind,
                    }
                    frame = _Frame.new(TYPE_FILE_CHUNK,
                                      data=json.dumps(meta, ensure_ascii=False, separators=(",", ":")),
                                      payload=chunk)
                    self.chunk_ids.add(frame.msg_id)
                    if not self.server.send_to_app(frame, track_ack=False):
                        self._finish("failed", reason="客户端断开或发送失败")
                        return
                    self.sent_chunks = seq + 1
                    self.progress(self.file_id, self.sent_chunks * len(chunk), self.total)
                    seq += 1
            if self.state == "sending":
                self.state = "waiting_ack"
                self._await_acks()
        except Exception as e:
            self._finish("failed", reason=str(e))

    def _await_acks(self):
        deadline = time.monotonic() + FILE_ACK_DEADLINE_SEC
        while self.acked_chunks < self.total_chunks:
            if self._cancel_evt.is_set():
                self._finish("cancelled")
                return
            if time.monotonic() > deadline:
                self._finish("failed", reason=f"分片确认超时（{self.acked_chunks}/{self.total_chunks}）")
                return
            time.sleep(0.05)
        # 分片在发送阶段就已被全部 ACK 时, 循环不执行, 这里补触发完成(幂等)
        self._finish("done")

    def _finish(self, state, reason=None):
        # 幂等: 只允许从进行中状态转为终态一次, 避免重复完成/回调
        with self._finish_lock:
            if self.state in ("done", "failed", "cancelled"):
                return
            self.state = state
        if state == "failed" and self.on_failed:
            try:
                self.on_failed(self.file_id, reason or "失败")
            except Exception:
                pass
        if state == "done" and self.on_done:
            try:
                self.on_done(self.file_id)
            except Exception:
                pass
        self.server._drop_file_transfer(self.file_id)

    def start(self):
        self._thread = threading.Thread(target=self.run, daemon=True,
                                        name=f"lobo-file-{self.file_id[:8]}")
        self._thread.start()

    def cancel(self, notify_peer=True):
        if self.state in ("done", "failed"):
            return
        self._cancel_evt.set()
        self._pause_evt.clear()
        if notify_peer:
            try:
                ctl = {"file_id": self.file_id, "action": "cancel"}
                self.server.send_to_app(_Frame.new(TYPE_FILE_CTRL,
                                                  data=json.dumps(ctl, ensure_ascii=False,
                                                                  separators=(",", ":"))))
            except Exception:
                pass
        self._finish("cancelled")

    def ack_chunk(self, msg_id):
        if msg_id in self.chunk_ids:
            self.chunk_ids.discard(msg_id)
            self.acked_chunks += 1
            if self.state == "waiting_ack" and self.acked_chunks >= self.total_chunks:
                self._finish("done")


# --------------------------------------------------------------------------- #
# Session
# --------------------------------------------------------------------------- #
class _Session:
    def __init__(self, server, conn, addr):
        self.server = server
        self.conn = conn
        self.addr = addr
        self.send_lock = threading.Lock()
        self.closed = False
        # 是否为桌宠/CLI 的本机客户端(收到 TYPE_LOCAL hello 后置 True), 默认视为 APP 设备
        self.is_local = False

    def send_frame(self, frame, progress=None):
        if self.closed:
            return False
        hb = json.dumps({
            "msg_id": frame.msg_id, "type": frame.type, "timestamp": frame.timestamp,
            "data": frame.data, "payload_size": frame.payload_size,
        }, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        total = frame.payload_size
        with self.send_lock:
            if self.closed:
                return False
            try:
                self.conn.sendall(struct.pack(">I", len(hb)))
                self.conn.sendall(hb)
                sent = 0
                payload = frame.payload
                while sent < total:
                    chunk = payload[sent:sent + SEND_CHUNK]
                    self.conn.sendall(chunk)
                    sent += len(chunk)
                    if progress:
                        progress(sent, total)
                if progress:
                    progress(total, total)
                return True
            except OSError:
                self._close()
                return False

    def _close(self):
        if self.closed:
            return
        self.closed = True
        try:
            self.conn.close()
        except OSError:
            pass
        self.server.on_session_closed(self)

    def run(self):
        reader = _FrameReader(self.conn)
        try:
            while True:
                frame = reader.next_frame(large_dir=self.server.large_dir)
                try:
                    self.server.on_frame(self, frame)
                finally:
                    frame.cleanup_payload()
        except EOFError:
            pass
        except (OSError, ValueError) as e:
            _log.warning("[%s] 会话异常: %s", self.addr[0], e)
        finally:
            self._close()


# --------------------------------------------------------------------------- #
# Server
# --------------------------------------------------------------------------- #
class _Server:
    def __init__(self, name="lobo-PC", tcp_port=DEFAULT_TCP_PORT,
                 udp_port=DEFAULT_UDP_PORT, hooks=None, large_dir=None):
        self.name = name
        self.tcp_port = tcp_port
        self.udp_port = udp_port
        self.large_dir = large_dir or tempfile.gettempdir()
        self.hooks = hooks or {}
        self.sessions = {}
        self.sessions_lock = threading.Lock()
        self.ack = _AckTracker(on_timeout=self._on_send_timeout)
        self.file_transfers = {}
        self.file_transfers_lock = threading.Lock()
        self._stop = threading.Event()
        self._tcp_thread = None
        self._udp_thread = None
        self._tcp_sock = None
        self._udp_sock = None

    def start(self):
        self._tcp_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._tcp_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._tcp_sock.bind(("0.0.0.0", self.tcp_port))
        self._tcp_sock.listen(8)
        self._tcp_sock.settimeout(0.5)
        _log.info("TCP 监听 0.0.0.0:%d (%s)", self.tcp_port, self.name)

        self._udp_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._udp_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            self._udp_sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        except OSError:
            pass
        self._udp_sock.bind(("0.0.0.0", self.udp_port))
        self._udp_sock.settimeout(0.5)
        _log.info("UDP 发现 0.0.0.0:%d", self.udp_port)

        self._tcp_thread = threading.Thread(target=self._tcp_accept_loop, name="tcp-accept")
        self._udp_thread = threading.Thread(target=self._udp_loop, name="udp-discover")
        self.ack.start_monitor(self._stop)
        self._tcp_thread.start()
        self._udp_thread.start()

    def stop(self):
        self._stop.set()
        for s in self._sessions_snapshot():
            s._close()
        for sock in (self._tcp_sock, self._udp_sock):
            if sock:
                try:
                    sock.close()
                except OSError:
                    pass

    def _sessions_snapshot(self):
        with self.sessions_lock:
            return list(self.sessions.values())

    def _tcp_accept_loop(self):
        while not self._stop.is_set():
            try:
                conn, addr = self._tcp_sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            try:
                conn.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, SOCKET_BUF)
                conn.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, SOCKET_BUF)
            except OSError:
                pass
            s = _Session(self, conn, addr)
            with self.sessions_lock:
                self.sessions[addr] = s
            threading.Thread(target=s.run, daemon=True, name=f"session-{addr[0]}").start()
            _log.info("[%s] 设备已连接（当前 %d 个）", addr[0], len(self.sessions))

    def _udp_loop(self):
        while not self._stop.is_set():
            try:
                data, addr = self._udp_sock.recvfrom(1024)
            except socket.timeout:
                continue
            except OSError:
                break
            if data.strip() == DISCOVER_MAGIC:
                resp = json.dumps({"t": "bailuobo_resp", "name": self.name, "port": self.tcp_port}).encode("utf-8")
                try:
                    self._udp_sock.sendto(resp, addr)
                except OSError:
                    pass

    def on_frame(self, session, frame):
        if frame.type == TYPE_ACK:
            self.ack.resolve(frame.data)
            self._ack_chunk(frame.data)
            return
        session.send_frame(_Frame.new(TYPE_ACK, data=frame.msg_id))
        if frame.type == TYPE_FILE_CTRL:
            self._handle_file_ctrl(frame)
        self._dispatch(session, frame)

    def _handle_file_ctrl(self, frame):
        try:
            ctl = json.loads(frame.data or "{}")
        except (json.JSONDecodeError, ValueError):
            return
        file_id = ctl.get("file_id")
        action = ctl.get("action")
        if not file_id:
            return
        t = self.file_transfers.get(file_id)
        if t is None:
            return
        if action == "cancel":
            t.cancel(notify_peer=False)

    def _ack_chunk(self, msg_id):
        with self.file_transfers_lock:
            t = next((t for t in self.file_transfers.values() if msg_id in t.chunk_ids), None)
        if t is not None:
            t.ack_chunk(msg_id)

    def _dispatch(self, session, frame):
        handler = self.hooks.get(frame.type)
        if handler:
            try:
                handler(session, frame)
            except Exception as e:
                _log.error("业务回调 %s 异常: %s", frame.type, e)
            return
        if frame.type == TYPE_TEXT:
            _log.info("[%s] TEXT: %s", session.addr[0], frame.data[:200])
        elif frame.type == TYPE_CLIPBOARD:
            _log.info("[%s] CLIPBOARD len=%d", session.addr[0], len(frame.data))
        elif frame.type == TYPE_TOOL:
            _log.info("[%s] TOOL %s %dB", session.addr[0], frame.data, frame.payload_size)

    def on_session_closed(self, session):
        with self.sessions_lock:
            self.sessions.pop(session.addr, None)
        _log.info("[%s] 设备断开（剩余 %d 个）", session.addr[0], len(self.sessions))
        if not self.sessions:
            with self.file_transfers_lock:
                doomed = list(self.file_transfers.values())
                self.file_transfers.clear()
            for t in doomed:
                if t.state in ("sending", "paused", "waiting_ack"):
                    t._finish("failed", reason="设备已断开")

    def _drop_file_transfer(self, file_id):
        with self.file_transfers_lock:
            self.file_transfers.pop(file_id, None)

    def _on_send_timeout(self, msg_id):
        _log.warning("发送超时未收到 ACK: %s", msg_id)

    def _sessions_matching(self, want_local):
        return [s for s in self._sessions_snapshot() if bool(s.is_local) == bool(want_local)]

    def send_to_app(self, frame, progress=None, track_ack=True):
        """发送给 APP 设备会话(排除本机客户端)"""
        ok = 0
        if track_ack:
            self.ack.register(frame.msg_id)
        for s in self._sessions_matching(False):
            if s.send_frame(frame, progress):
                ok += 1
        return ok

    def send_to_local(self, frame):
        """发送给本机客户端会话(桌宠/CLI)"""
        ok = 0
        for s in self._sessions_matching(True):
            if s.send_frame(frame):
                ok += 1
        return ok

    def send_text(self, text):
        """向 APP 发送文本"""
        return self.send_to_app(_Frame.new(TYPE_TEXT, data=text))

    def send_file_chunked(self, path, msg_type=TYPE_TOOL, progress=None,
                          on_done=None, on_failed=None):
        file_id = uuid.uuid4().hex
        t = _OutgoingFileTransfer(self, file_id, path, kind=msg_type,
                                 progress=progress, on_done=on_done, on_failed=on_failed)
        with self.file_transfers_lock:
            self.file_transfers[file_id] = t
        t.start()
        _log.info("分片发送 fid=%s path=%s size=%d", file_id[:8], path, t.total)
        return file_id

    def cancel_file(self, file_id):
        t = self.file_transfers.get(file_id)
        if t:
            t.cancel()
            return True
        return False


# --------------------------------------------------------------------------- #
# 文件接收（内联 file_service，简化，只保留核心功能）
# --------------------------------------------------------------------------- #
_receivers: dict = {}
_receivers_lock = threading.Lock()


def _safe_name(name: str) -> str:
    name = os.path.basename(name) or "file"
    return "".join(c for c in name if c not in '\\/:*?"<>|')


def _unique(dst: Path) -> Path:
    if not dst.exists():
        return dst
    base, ext = dst.stem, dst.suffix
    i = 1
    while True:
        cand = dst.with_name(f"{base}_{i}{ext}")
        if not cand.exists():
            return cand
        i += 1


def _on_file_chunk(session, frame, on_event=None):
    """大文件分片重组; on_event 用于上报接收进度(供桌宠显示进度气泡)"""
    try:
        meta = json.loads(frame.data or "{}")
    except (json.JSONDecodeError, ValueError):
        return
    fid = meta.get("file_id")
    if not fid:
        return
    seq = int(meta.get("seq", 0))
    total = int(meta.get("total", 1))
    size = int(meta.get("size", 0) or 0)
    name = _safe_name(meta.get("name") or f"file_{fid[:8]}.bin")
    payload = frame.payload if frame.payload is not None else frame.read_payload()

    with _receivers_lock:
        r = _receivers.get(fid)
        if r is None or seq == 0:
            if r is not None:
                try:
                    r["tmp"].unlink(missing_ok=True)
                except OSError:
                    pass
            tmp = _output_dir() / f".{name}.{fid[:8]}.part"
            r = {"tmp": tmp, "name": name, "last": seq, "total": total, "size": size, "got": 0}
            _receivers[fid] = r
        elif seq <= r["last"]:
            return
        else:
            r["last"] = seq
            r["total"] = total
            if size:
                r["size"] = size
        r["got"] = r.get("got", 0) + len(payload or b"")
        got, total_size = r["got"], r.get("size", 0)

    try:
        with open(r["tmp"], "ab") as f:
            f.write(payload or b"")
    except OSError:
        return

    if on_event:
        if total_size:
            pct = int(got / total_size * 100)
        else:
            pct = int((seq + 1) / max(total, 1) * 100)
        on_event({"kind": "progress", "dir": "recv", "name": name,
                  "pct": min(pct, 100), "file_id": fid})

    if seq >= total - 1:
        dst = _unique(_output_dir() / r["name"])
        try:
            os.replace(r["tmp"], dst)
            _log.info("文件已保存: %s", dst)
        except OSError:
            pass
        with _receivers_lock:
            _receivers.pop(fid, None)
        if on_event:
            on_event({"kind": "done", "dir": "recv", "name": r["name"], "file_id": fid})


def _on_file_ctrl(session, frame, on_event=None):
    """分片控制: cancel"""
    try:
        ctl = json.loads(frame.data or "{}")
    except (json.JSONDecodeError, ValueError):
        return
    if ctl.get("action") != "cancel" or not ctl.get("file_id"):
        return
    with _receivers_lock:
        r = _receivers.pop(ctl["file_id"], None)
    if r:
        try:
            r["tmp"].unlink(missing_ok=True)
        except OSError:
            pass
    if on_event:
        on_event({"kind": "failed", "dir": "recv", "name": (r or {}).get("name", "file"),
                  "reason": "对方已取消", "file_id": ctl.get("file_id")})


def _on_android_tool(session, frame, on_event=None):
    """单帧文件（≤16MB）保存"""
    name = _safe_name(frame.data or "tool.bin")
    dst = _unique(_output_dir() / name)
    try:
        dst.write_bytes(frame.read_payload())
        _log.info("文件已保存: %s", dst)
        if on_event:
            on_event({"kind": "done", "dir": "recv", "name": name})
    except Exception as e:
        _log.warning("文件保存失败: %s", e)
        if on_event:
            on_event({"kind": "failed", "dir": "recv", "name": name, "reason": str(e)})


# --------------------------------------------------------------------------- #
# 本机客户端(桌宠/CLI 侧): 回环连接守护进程, 复用 _Frame 协议
# --------------------------------------------------------------------------- #
class _LocalClient:
    """连接 lobo 守护进程的本机客户端; on_event 在后台线程被回调"""

    def __init__(self, host=LOCAL_HOST, port=DEFAULT_TCP_PORT, on_event=None):
        self.host = host
        self.port = port
        self.on_event = on_event or (lambda ev: None)
        self.sock = None
        self.reader = None
        self.connected = False
        self.send_lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None

    def start(self):
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._connect_loop, daemon=True, name="lobo-local")
        self._thread.start()

    def _connect_loop(self):
        while not self._stop.is_set():
            try:
                s = socket.create_connection((self.host, self.port), timeout=2)
                try:
                    s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                except OSError:
                    pass
                self.sock = s
                self.reader = _FrameReader(s)
                self.connected = True
                self._send(_Frame.new(TYPE_LOCAL, data=json.dumps({"cmd": "hello"})))
                self._read_loop()
            except Exception:
                pass
            finally:
                self.connected = False
                self.sock = None
                self.reader = None
            self._stop.wait(2)  # 服务未就绪或断开时 2 秒后重连

    def _read_loop(self):
        while not self._stop.is_set():
            frame = self.reader.next_frame()
            if frame.type != TYPE_LOCAL:
                continue
            try:
                ev = json.loads(frame.data or "{}")
            except (json.JSONDecodeError, ValueError):
                continue
            if isinstance(ev, dict):
                try:
                    self.on_event(ev)
                except Exception:
                    pass

    def _send(self, frame):
        with self.send_lock:
            if self.sock is None:
                return False
            try:
                self.sock.sendall(frame.to_bytes())
                return True
            except OSError:
                return False

    def send_text(self, text):
        """发送文本到 APP"""
        return self._send(_Frame.new(TYPE_LOCAL, data=json.dumps(
            {"cmd": "send_text", "text": text}, ensure_ascii=False)))

    def send_file(self, path, kind=TYPE_TOOL):
        """把文件交给守护进程, 由守护进程分片转发给 APP"""
        try:
            payload = Path(path).read_bytes()
        except OSError:
            return False
        msg = {"cmd": "send_file", "name": os.path.basename(path), "kind": kind}
        return self._send(_Frame.new(TYPE_LOCAL,
                                     data=json.dumps(msg, ensure_ascii=False), payload=payload))

    def cancel_file(self, file_id):
        return self._send(_Frame.new(TYPE_LOCAL, data=json.dumps(
            {"cmd": "cancel_file", "file_id": file_id}, ensure_ascii=False)))

    def stop(self):
        self._stop.set()
        s, self.sock = self.sock, None
        if s is not None:
            try:
                s.close()
            except OSError:
                pass


def _rm(path):
    try:
        Path(path).unlink(missing_ok=True)
    except OSError:
        pass


def _handle_local(session, frame, server, to_local):
    """守护进程侧: 处理本机客户端请求(hello / send_text / send_file / cancel_file)"""
    session.is_local = True
    try:
        msg = json.loads(frame.data or "{}")
    except (json.JSONDecodeError, ValueError):
        return
    cmd = msg.get("cmd")
    if cmd == "hello":
        return
    if cmd == "send_text":
        text = msg.get("text") or ""
        if text:
            server.send_to_app(_Frame.new(TYPE_TEXT, data=text))
        return
    if cmd == "cancel_file":
        server.cancel_file(msg.get("file_id") or "")
        return
    if cmd != "send_file":
        return

    name = _safe_name(msg.get("name") or "file")
    kind = msg.get("kind") or TYPE_TOOL
    staging = _output_dir() / f".send_{uuid.uuid4().hex}.part"
    try:
        if frame.payload_path:
            shutil.copyfile(frame.payload_path, staging)
        else:
            data = frame.payload if frame.payload is not None else frame.read_payload()
            staging.write_bytes(data or b"")
    except Exception as e:
        to_local({"kind": "failed", "dir": "send", "name": name, "reason": str(e)})
        return

    def _prog(fid, sent, total):
        to_local({"kind": "progress", "dir": "send", "name": name,
                  "pct": int(min(100, sent / max(total, 1) * 100)), "file_id": fid})

    def _done(fid):
        to_local({"kind": "done", "dir": "send", "name": name, "file_id": fid})
        _rm(staging)

    def _fail(fid, reason):
        to_local({"kind": "failed", "dir": "send", "name": name, "reason": reason, "file_id": fid})
        _rm(staging)

    try:
        server.send_file_chunked(str(staging), kind,
                                 progress=_prog, on_done=_done, on_failed=_fail)
    except Exception as e:
        to_local({"kind": "failed", "dir": "send", "name": name, "reason": str(e)})
        _rm(staging)


def _make_hooks(server):
    """守护进程业务回调: 转发 APP<->本机, 并处理文件接收/进度上报"""

    def to_local(event):
        frame = _Frame.new(TYPE_LOCAL, data=json.dumps(event, ensure_ascii=False,
                                                       separators=(",", ":")))
        server.send_to_local(frame)

    def _on_incoming_text(session, frame):
        """APP 发来的文本 / 剪贴板内容: 统一转发给本机客户端(桌宠展示并生成"〇"按钮)"""
        if session.is_local:
            return
        text = (frame.data or "").strip()
        if text:
            to_local({"kind": "text", "text": text})

    def _on_tool(session, frame):
        if session.is_local:
            return
        _on_android_tool(session, frame, on_event=to_local)

    def _on_chunk(session, frame):
        if session.is_local:
            return
        _on_file_chunk(session, frame, on_event=to_local)

    def _on_ctrl(session, frame):
        if session.is_local:
            return
        _on_file_ctrl(session, frame, on_event=to_local)

    def _on_local(session, frame):
        _handle_local(session, frame, server, to_local)

    return {TYPE_TEXT: _on_incoming_text, TYPE_CLIPBOARD: _on_incoming_text,
            TYPE_TOOL: _on_tool, TYPE_FILE_CHUNK: _on_chunk,
            TYPE_FILE_CTRL: _on_ctrl, TYPE_LOCAL: _on_local}


def serve():
    """守护进程入口: sun lobo serve (阻塞保活, 由 cmd 统一启停)

    下载目录在每次用到时现取(见 _output_dir), 因此不依赖本函数何时被调用
    """
    server = _Server(name="lobo-PC", tcp_port=DEFAULT_TCP_PORT, udp_port=DEFAULT_UDP_PORT)
    server.hooks = _make_hooks(server)
    try:
        server.start()
    except OSError as e:
        # 端口被占等启动失败: 记录原因并退出, 不静默消失
        # (traceback 走 stderr, 由 cmd 落到 log/<服务名>.stderr.log)
        _log.exception("lobo 守护进程启动失败 (tcp=%d, udp=%d)", DEFAULT_TCP_PORT, DEFAULT_UDP_PORT)
        _sun_log(f"lobo 守护进程启动失败 (tcp={DEFAULT_TCP_PORT}, udp={DEFAULT_UDP_PORT}): "
                 f"[Errno {e.errno}] {e.strerror or e}")
        try:
            server.stop()
        except Exception:
            pass
        raise SystemExit(1)
    _sun_log(f"lobo 守护进程已就绪 (pid={os.getpid()}, 下载目录={_output_dir()})")

    stop_evt = threading.Event()

    def _on_signal(signum, _frame):
        _sun_log(f"lobo 守护进程收到信号 {signum}, 准备退出")
        stop_evt.set()

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(sig, _on_signal)
        except Exception:
            pass

    try:
        while not stop_evt.wait(0.5):
            pass
    finally:
        server.stop()
        _sun_log("lobo 守护进程已退出")


# --------------------------------------------------------------------------- #
# lobo 模块主类
# --------------------------------------------------------------------------- #
class Lobo:
    # tool() 自行往工具面板里放按钮, 不需要 mgmg 再自动生成一个同名按钮
    tool_self_managed = True

    def __init__(self, G):
        self.g = G
        self.name = ["lobo", "lobo server"]
        self.run_methods = ["run", "stop", "serve"]
        self.call_is_runnig = False
        self._client = None        # 本机客户端(连接守护进程)
        self._mounted = False      # 是否已重载剪贴板 tool()
        self._send_fid = None      # 当前发送中的文件 id(用于右键取消)
        self._send_done = None     # CLI 发送时的完成事件

    # -------------------- 辅助 --------------------

    def _mgmg(self):
        """安全获取 mgmg 实例(CLI 环境下 GUI 模块可能未加载)"""
        return getattr(self.g, "mgmg", None)

    def _is_service_child(self) -> bool:
        """当前进程是否为 cmd 拉起的 lobo 守护进程(而非 P0 / CLI / GUI 进程)"""
        try:
            return self.g.cmd.service_identity() == SERVICE_NAME
        except Exception:
            return False

    # -------------------- 服务注册与状态 --------------------

    def _register_service(self):
        """向 cmd 注册 lobo 后台服务

        argv 走统一入口 sun_cli, 服务进程内也会完成 load_all/call(),
        因此守护进程同样持有 G 与 config(下载目录、日志都随之可用)
        """
        try:
            sun_cli = Path(__file__).resolve().parent.parent / "sun_cli.py"
            self.g.cmd.register_service(
                SERVICE_NAME,
                [sys.executable, str(sun_cli), SERVICE_NAME, "serve"],
                marker="sun_cli.py",
            )
        except Exception as e:
            self.g.log.run(f"lobo 服务注册失败: {e}")

    def _is_running(self) -> bool:
        try:
            return bool(self.g.cmd.service_status(SERVICE_NAME).get("running"))
        except Exception:
            return False

    # -------------------- CLI: stop --------------------

    def stop(self):
        """sun lobo stop: 已打开才关闭(仅关闭 lobo 服务, 不影响桌宠)"""
        if self._is_running():
            self.g.cmd.stop_service(SERVICE_NAME)
            self.g.log.run("lobo 服务已停止")
        else:
            self.g.log.run("lobo 服务未运行")

    # -------------------- CLI: run / serve / 发送文件 --------------------

    def run(self, *args):
        """sun lobo run 启动服务; sun lobo serve 前台进入守护循环(服务子进程入口); sun lobo <文件> 发送文件"""
        action = args[0] if args else "run"

        if action == "serve":
            serve()
            return

        if action == "run":
            if self._is_running():
                self.g.log.run("lobo 服务已在运行")
            elif self.g.cmd.start_service(SERVICE_NAME):
                self.g.log.run(f"lobo 服务已启动 (TCP:{DEFAULT_TCP_PORT} UDP:{DEFAULT_UDP_PORT})")
            else:
                self.g.log.run("lobo 服务启动失败")
            return

        file_path = action
        if not os.path.exists(file_path):
            self.g.log.run(f"lobo 文件不存在: {file_path}")
            return
        threading.Thread(target=self._send_file, args=(file_path,), daemon=True).start()

    def _ensure_client(self):
        """确保本机客户端已连接(服务未运行则先拉起)"""
        if not self._is_running():
            self.g.cmd.start_service(SERVICE_NAME)
        if self._client is None:
            self._client = _LocalClient(on_event=self._on_local_event)
            self._client.start()
        return self._client

    def _send_file(self, file_path):
        name = os.path.basename(file_path)
        client = self._ensure_client()
        for _ in range(50):  # 最多等 5 秒连接就绪
            if client.connected:
                break
            time.sleep(0.1)
        if not client.connected:
            self._cli_out(f"lobo 发送失败: 无法连接服务 ({name})")
            return
        self._send_done = threading.Event()
        if not client.send_file(file_path, TYPE_TOOL):
            self._cli_out(f"lobo 文件发送失败: {name}")
            return
        self._cli_out(f"lobo 开始发送: {name}")
        if getattr(self._mgmg(), "root", None) is None:
            self._send_done.wait(timeout=300)  # CLI 下阻塞等待, 保证进度输出完整
        self._send_done = None

    # -------------------- call: 默认后台起服务 + 接入本机客户端 --------------------

    def call(self):
        """启动时初始化: 注册服务; 是否唤醒由 P0 按 config 统一决定

        - 定义与策略分离: 这里只 register_service, 启不启由 P0 查 lobo_auto_start 决定
        - 下载目录不在此时缓存, 每次用到时由 _output_dir() 现取 config
        - 服务子进程(sun lobo serve): 不接 UI, 随后由 run("serve") 进入阻塞主循环
        - 无 GUI 的进程(P0 / sun lobo run|stop): 收文件进度与剪贴板联动都需要界面, 故不接客户端
        """
        if self.call_is_runnig:
            return

        self._register_service()
        self.call_is_runnig = True

        if self._is_service_child():
            return

        if getattr(self._mgmg(), "root", None) is None:
            return

        # 本机客户端: 接收 APP 文本/文件进度, 联动剪贴板与进度气泡
        self._client = _LocalClient(on_event=self._on_local_event)
        self._client.start()

        # 通过 G 重载/挂载: 点击剪贴板按钮时, 同时把文本发送到 APP
        self._mount_clipboard()

    # -------------------- 依赖剪贴板模块: 挂载 tool (重载覆盖) --------------------

    def _mount_clipboard(self):
        """重载剪贴板模块 tool(): 点击剪贴板按钮时, 除生成"〇"按钮外尝试发送文本到 APP"""
        cb = getattr(self.g, "clipboard", None)
        if cb is None or self._mounted or not callable(getattr(cb, "tool", None)):
            return
        orig_tool = cb.tool

        def patched_tool():
            orig_tool()  # 原逻辑: 读取剪贴板并生成"〇"按钮
            try:
                text = self.g.clipboard.get_text()
                if text:
                    self.send_text(text)
            except Exception as e:
                self.g.log.run(f"lobo 剪贴板发送失败: {e}")

        cb.tool = patched_tool
        self._mounted = True

    # -------------------- 发送文本到 APP --------------------

    def send_text(self, text):
        """经守护进程把文本发送到 APP; 供剪贴板按钮调用"""
        text = (text or "").strip()
        if not text:
            return False
        client = self._ensure_client()
        for _ in range(20):  # 最多等 1 秒
            if client.connected:
                break
            time.sleep(0.05)
        if not client.connected:
            self._notify("lobo 服务未连接")
            return False
        if client.send_text(text):
            self._notify(f"剪贴板已发送（{len(text)}字）")
            try:
                self.g.log.run(f"lobo 剪贴板已发送: {text[:40]}")
            except Exception:
                pass
            return True
        self._notify("发送失败")
        return False

    # -------------------- 本机客户端事件回调(后台线程) --------------------

    def _on_local_event(self, ev):
        kind = ev.get("kind")
        if kind == "text":
            text = ev.get("text") or ""
            if text:
                self._ui(lambda: self._on_recv_text(text))
            return
        self._ui(lambda: self._on_transfer_event(ev))
        if kind in ("done", "failed") and self._send_done is not None:
            self._send_done.set()

    def _on_recv_text(self, text):
        """后台服务收到 APP 文本 → 调用剪贴板模块生成"〇"按钮"""
        try:
            self.g.clipboard.add(text)
            self.g.log.run(f"lobo 收到文本, 已生成剪贴板按钮 (len={len(text)})")
            self.g.mgmg.dm_print(f"收到文本: {text[:20]}{'…' if len(text) > 20 else ''}", 3000)
        except Exception as e:
            self.g.log.run(f"lobo 收到文本处理失败: {e}")

    def _on_transfer_event(self, ev):
        """文件收发进度: 接收与发送使用同一种进度气泡"""
        kind = ev.get("kind")
        name = (ev.get("name") or "file")[:14]
        label = "接收" if ev.get("dir") == "recv" else "发送"
        mgmg = getattr(self.g, "mgmg", None)
        if mgmg is None or getattr(mgmg, "root", None) is None:
            if kind == "progress":
                self._cli_out(f"{label} {name} {ev.get('pct', 0)}%")
            elif kind == "done":
                self._cli_out(f"{label}完成: {name}")
            else:
                self._cli_out(f"{label}失败: {name} ({ev.get('reason', '')})")
            return
        try:
            if kind == "progress":
                self._ensure_panel()
                if ev.get("dir") == "send":
                    self._send_fid = ev.get("file_id")
                    mgmg.tool_print(f"{label} {name} {ev.get('pct', 0)}%", on_close=self._cancel_send)
                else:
                    mgmg.tool_print(f"{label} {name} {ev.get('pct', 0)}%")
            elif kind == "done":
                self._send_fid = None
                mgmg.tool_print_close()
                mgmg.dm_print(f"✓ {name} {label}完成", 2500)
            elif kind == "failed":
                self._send_fid = None
                mgmg.tool_print_close()
                mgmg.dm_print(f"✗ {name} {label}失败: {ev.get('reason', '')}", 3000)
        except Exception:
            pass

    def _cancel_send(self):
        fid, self._send_fid = self._send_fid, None
        if fid and self._client is not None:
            self._client.cancel_file(fid)
            self.g.log.run(f"lobo 已取消发送: {fid[:8]}")

    def _ensure_panel(self):
        """进度气泡需工具面板存在; 未打开时先打开, 保证进度可见"""
        mgmg = getattr(self.g, "mgmg", None)
        try:
            if mgmg is not None and not mgmg.is_widget_alive(mgmg.dm_tools_frame):
                mgmg.tools()
        except Exception:
            pass

    # -------------------- 线程/输出辅助 --------------------

    def _ui(self, fn):
        """有 GUI 时回到主线程执行; 否则当前线程执行"""
        root = getattr(self._mgmg(), "root", None)
        if root is not None:
            try:
                root.after(0, fn)
                return
            except Exception:
                pass
        try:
            fn()
        except Exception:
            pass

    def _notify(self, text):
        """有 GUI 弹气泡, 否则打印到终端"""
        if getattr(self._mgmg(), "root", None) is not None:
            try:
                self.g.mgmg.dm_print(text, 2000)
                return
            except Exception:
                pass
        print(f"[lobo] {text}")

    def _cli_out(self, text):
        try:
            self.g.log.run(text)
        except Exception:
            pass
        if getattr(self._mgmg(), "root", None) is None:
            print(f"[lobo] {text}")

    # -------------------- tool: GUI 工具按钮 --------------------

    def tool(self):
        """右键工具面板入口: 发送剪贴板按钮"""
        import tkinter as tk
        from tkinter import ttk

        def _send():
            text = self.g.clipboard.get_text()
            if not text:
                self.g.mgmg.dm_print("剪贴板为空", 1500)
                return
            self.send_text(text)

        ttk.Button(self.g.mgmg.dm_tools_frame, text="发送剪贴板",
                   command=_send).pack(fill=tk.X, padx=0, pady=4)