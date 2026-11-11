"""gui 的表情资源: 扫描 emo/0..5 下的图片/GIF/视频并渲染到主窗口

状态存放在 gui 应用实例(app.emo_*)上, 本文件只做渲染, 不依赖 core。
"""

import random
import time
import tkinter as tk
from threading import Thread

import cv2
import imageio
from PIL import Image, ImageTk

SUPPORTED_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".mp4", ".avi", ".mov", ".webm"}
VIDEO_EXTS = {".mp4", ".avi", ".mov", ".webm"}
EMO_SIZE = 150
EMOTIONS = 6
BG = "#123456"


def _alive(widget) -> bool:
    try:
        return widget is not None and widget.winfo_exists()
    except Exception:
        return False


def load(app):
    """扫描表情目录, 结果存入 app.emo_paths[0..5]"""
    base = app.emo_root()
    app.emo_paths = []
    for i in range(EMOTIONS):
        folder = base / str(i)
        paths = sorted(p for p in folder.iterdir() if p.suffix.lower() in SUPPORTED_EXTS) if folder.is_dir() else []
        app.emo_paths.append(paths)


def show(app, number=1):
    """显示第 number 组表情, 越界回退到 0"""
    if not _alive(app.root):
        return
    _cancel_timer(app)
    stop(app)
    if number < 0 or number >= len(app.emo_paths):
        number = 0
    paths = app.emo_paths[number] if app.emo_paths else []
    if not paths:
        # 整组目录都为空时不提示也不重试, 避免每个情绪周期都弹空气泡
        if any(app.emo_paths):
            app.bubble(f"情绪 {number} 没有资源", 3000)
        return

    label = _label(app)
    _bind(app, label)

    if len(paths) > 1 and app.emo_current in paths:
        paths = [p for p in paths if p != app.emo_current]
    path = random.choice(paths)
    app.emo_current = path

    ext = path.suffix.lower()
    try:
        if ext == ".gif":
            _play_gif(app, path, 120)
            app.emo_timer = app.root.after(120000, lambda: show(app, 0))
        elif ext in VIDEO_EXTS:
            _play_video(app, path)
        else:
            _paint(app, _fit(Image.open(path)))
            app.emo_timer = app.root.after(120000, lambda: show(app, 0))
    except Exception as e:
        app.bubble(f"加载失败: {e}", 3000)
        app.emo_timer = app.root.after(0, lambda: show(app, 0))


def stop(app):
    """停止当前播放并清空画面"""
    app.emo_playing = False
    app.emo_token += 1
    _cancel_timer(app)
    if app.emo_capture is not None:
        app.emo_capture.release()
        app.emo_capture = None
    if app.emo_thread is not None and app.emo_thread.is_alive():
        app.emo_thread.join(timeout=0.5)
    app.emo_thread = None
    if _alive(app.emo_label):
        try:
            app.emo_label.config(image="")
        except tk.TclError:
            pass
    app.emo_photo = None


def close(app):
    """关闭表情显示, 清理资源"""
    stop(app)
    if app.emo_label is not None:
        try:
            app.emo_label.pack_forget()
        except tk.TclError:
            pass
        app.emo_label = None
    app.emo_current = None


# ---------------------------------------------------------------- 内部实现


def _label(app):
    if app.emo_label is None:
        app.emo_label = tk.Label(app.root, bg=BG)
    app.emo_label.pack(expand=True, fill="both")
    return app.emo_label


def _bind(app, label):
    """表情铺满窗口, 事件必须绑到 label 上, 并返回 break 阻止冒泡到 root"""
    for seq, handler in (("<Button-1>", app.on_press),
                         ("<B1-Motion>", app.on_drag),
                         ("<ButtonRelease-1>", app.on_release),
                         ("<Button-3>", app.tools)):
        def wrap(event, h=handler):
            h(event)
            return "break"
        label.bind(seq, wrap)


def _fit(img):
    w, h = img.size
    scale = min(EMO_SIZE / w, EMO_SIZE / h)
    return img.resize((int(w * scale), int(h * scale)), Image.Resampling.LANCZOS)


def _paint(app, img):
    if not _alive(app.emo_label):
        return
    app.emo_photo = ImageTk.PhotoImage(img)
    app.emo_label.config(image=app.emo_photo)


def _cancel_timer(app):
    if app.emo_timer is not None:
        try:
            app.root.after_cancel(app.emo_timer)
        except tk.TclError:
            pass
        app.emo_timer = None


def _frame_delay(reader, default=0.08):
    """读取 GIF 每帧时长(秒), 缺失或非法时回退 default"""
    try:
        duration = reader.get_meta_data().get("duration")
    except Exception:
        return default
    if isinstance(duration, (list, tuple)):
        duration = duration[0] if duration else None
    if not duration:
        return default
    delay = duration / 1000.0 if duration >= 1 else float(duration)
    return delay if delay > 0 else default


def _play_gif(app, path, seconds):
    stop(app)
    token = app.emo_token
    start = time.time()
    try:
        reader = imageio.get_reader(path)
        delay = _frame_delay(reader)
    except Exception as e:
        app.bubble(f"无法播放GIF: {e}", 10000)
        return

    def loop():
        try:
            while token == app.emo_token and time.time() - start < seconds:
                for frame in reader:
                    if token != app.emo_token or time.time() - start >= seconds:
                        break
                    img = _fit(Image.fromarray(frame))
                    app.root.after(0, lambda im=img: _paint(app, im) if token == app.emo_token else None)
                    time.sleep(delay)
                if token == app.emo_token and time.time() - start < seconds:
                    reader.set_image_index(0)
        except Exception as e:
            app.root.after(0, lambda msg=str(e): app.bubble(f"GIF播放错误: {msg}", 10000))
        finally:
            reader.close()

    app.emo_playing = True
    app.emo_thread = Thread(target=loop, daemon=True)
    app.emo_thread.start()


def _play_video(app, path):
    stop(app)
    token = app.emo_token

    def loop():
        cap = None
        try:
            cap = cv2.VideoCapture(path)
            app.emo_capture = cap
            if not cap.isOpened():
                app.root.after(0, lambda: app.bubble("无法打开视频文件", 3000))
                return
            delay = 1.0 / (cap.get(cv2.CAP_PROP_FPS) or 30)
            while token == app.emo_token:
                ok, frame = cap.read()
                if not ok:
                    break
                img = _fit(Image.fromarray(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)))
                app.root.after(0, lambda im=img: _paint(app, im) if token == app.emo_token else None)
                time.sleep(delay)
        except Exception as e:
            app.root.after(0, lambda msg=str(e): app.bubble(f"视频播放错误: {msg}", 3000))
        finally:
            if cap is not None:
                cap.release()
            if app.emo_capture is cap:
                app.emo_capture = None
            if token == app.emo_token:
                app.emo_playing = False
                app.root.after(0, lambda: show(app, 0))

    app.emo_playing = True
    app.emo_thread = Thread(target=loop, daemon=True)
    app.emo_thread.start()
