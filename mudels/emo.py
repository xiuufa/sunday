"""
emo 表情显示模块
- 从 <config.emo_path>/0..5 目录加载表情资源(图片/GIF/视频), 路径每次使用时现取
- 对外接口: load_emo / emo / emo_close / stop_video / play_gif / play_video
- 依赖 mgmg 的 root/label; 自身状态(播放代际等)由本模块在 G 上挂载, 不依赖外部预初始化
"""
import os
import pathlib
import random
import time
import tkinter as tk
from threading import Thread

import cv2
import imageio
from PIL import Image, ImageTk

# 支持的表情资源后缀
SUPPORTED_EXTS = {'.png', '.jpg', '.jpeg', '.gif', '.mp4', '.avi', '.mov', '.webm'}

# 表情显示区域尺寸(等比缩放)
EMO_SIZE = 150

# 本模块在 G 上挂载的运行期状态及默认值
_STATE_DEFAULTS = {
    "data_emo_path_list": [],
    "emo_label": None,
    "emo_timer": None,
    "current_photo_path": None,
    "current_video_path": None,
    "video_capture": None,
    "video_playing": False,
    "video_thread": None,
    "video_token": 0,  # 表情播放代际: 用于判定哪次播放有权更新画面
}


def _ensure_state(g):
    """补齐本模块在 G 上的运行期状态(幂等): 只挂自己的状态, 不缓存别人的"""
    for name, value in _STATE_DEFAULTS.items():
        if not hasattr(g, name):
            setattr(g, name, value)
    g.ImageTk = ImageTk


def _base_path(g) -> str:
    """表情资源根目录: 使用时现取 config.emo_path, 绝对路径失效时退化为项目默认路径"""
    try:
        raw = g.config.get("emo_path", "data/emo")
    except Exception:
        raw = None
    if raw and os.path.isabs(raw) and os.path.exists(raw):
        return raw
    fallback = str(pathlib.Path(__file__).resolve().parent.parent / "data" / "emo")
    # 退化时记录原因, 便于排查配置里的绝对路径为何失效
    try:
        g.log.run(f"config emo_path 不可用({raw!r}), 已退化为默认路径: {fallback}")
    except Exception:
        pass
    return fallback


def load_emo(g):
    """扫描表情根目录下的 0..5 子目录, 把每个情绪的资源路径存入 g.data_emo_path_list"""
    _ensure_state(g)
    base = _base_path(g)
    g.data_emo_path_list = []
    for i in range(6):
        paths = []
        folder = os.path.join(base, str(i))
        if os.path.isdir(folder):
            for name in os.listdir(folder):
                if os.path.splitext(name)[1].lower() in SUPPORTED_EXTS:
                    paths.append(os.path.join(folder, name))
        g.data_emo_path_list.append(paths)


def _fit_image(img):
    """把图片等比缩放到 EMO_SIZE 内"""
    w, h = img.size
    scale = min(EMO_SIZE / w, EMO_SIZE / h)
    return img.resize((int(w * scale), int(h * scale)), Image.Resampling.LANCZOS)


def _bind_emo_events(mgmg, label):
    """表情铺满主窗口, 必须把鼠标事件绑到 label 上, 否则点击/拖拽无效
    处理器返回 "break" 阻止事件继续传播到 root, 避免同一事件被触发两次(如右键闪一下消失)
    """
    for seq, name in (('<Button-1>', 'on_press'),
                      ('<B1-Motion>', 'on_drag'),
                      ('<ButtonRelease-1>', 'on_release')):
        handler = getattr(mgmg, name, None)
        if callable(handler):
            def wrap(ev, h=handler):
                h(ev)
                return "break"
            label.bind(seq, wrap)
    if callable(getattr(mgmg, 'tools', None)):
        def show_tools(e):
            mgmg.tools(e)
            return "break"
        label.bind('<Button-3>', show_tools)


def _next_token(g):
    """推进播放代际: 使在播线程与已排队的 after 回调同时失效"""
    g.video_token = getattr(g, "video_token", 0) + 1
    return g.video_token


def _is_current(g, token) -> bool:
    """token 是否仍持有播放权(每次 play_gif/play_video/stop_video 都会换代)"""
    return getattr(g, "video_token", 0) == token


def stop_video(g, mgmg):
    """停止当前表情播放(视频/GIF), 并清空画面

    仅靠 video_playing 无法真正停住: 旧线程可能还在 sleep, after(0) 排队的回调也可能尚未执行。
    这里推进代际, 让它们全部作废, 避免旧帧覆盖新画面
    """
    _ensure_state(g)
    g.video_playing = False
    _next_token(g)
    if g.emo_timer is not None:
        try:
            mgmg.root.after_cancel(g.emo_timer)
        except Exception:
            pass
        g.emo_timer = None
    if g.video_capture is not None:
        g.video_capture.release()
        g.video_capture = None
    if g.video_thread is not None and g.video_thread.is_alive():
        g.video_thread.join(timeout=0.5)
    g.video_thread = None
    if g.emo_label is not None:
        try:
            g.emo_label.config(image='')
            g.current_photo_path = None
        except tk.TclError:
            pass


def _gif_frame_delay(reader, default=0.08):
    """读取 GIF 每帧时长(秒); 缺失或非法时回退 default(旧行为 80ms)"""
    try:
        duration = reader.get_meta_data().get("duration")
    except Exception:
        return default
    if isinstance(duration, (list, tuple)):
        duration = duration[0] if duration else None
    if not duration:
        return default
    # imageio 的 GIF 元数据时长以毫秒返回(如 40); 兼容少数以秒返回(如 0.04)的情况
    delay = duration / 1000.0 if duration >= 1 else float(duration)
    return delay if delay > 0 else default


def play_gif(path, end_time, g, mgmg):
    """播放 GIF 表情, end_time 秒后由定时器切回默认表情"""
    stop_video(g, mgmg)
    token = _next_token(g)  # 本次播放的代际: 只有它有权更新画面

    def gif_loop():
        try:
            while _is_current(g, token) and g.video_playing and (time.time() - start_time < end_time):
                for frame in reader:
                    if (not _is_current(g, token) or not g.video_playing
                            or (time.time() - start_time >= end_time)):
                        break
                    img = _fit_image(Image.fromarray(frame))

                    def update_image(img1=img):
                        # 排队中的回调可能晚于下一次播放, 执行前必须再校验代际
                        if not _is_current(g, token):
                            return
                        g.current_photo_path = g.ImageTk.PhotoImage(img1)
                        g.emo_label.config(image=g.current_photo_path)

                    mgmg.root.after(0, update_image)
                    time.sleep(frame_delay)
                if _is_current(g, token) and g.video_playing and (time.time() - start_time < end_time):
                    reader.set_image_index(0)
        except Exception as e:
            mgmg.dm_print(f"GIF播放错误: {e}", 10000)
        finally:
            reader.close()
            # 只有仍持有播放权时才收起标志, 否则会误停刚接手的下一次播放
            if _is_current(g, token):
                g.video_playing = False

    g.video_playing = True
    start_time = time.time()
    try:
        reader = imageio.get_reader(path)
        frame_delay = _gif_frame_delay(reader)
    except Exception as e:
        mgmg.dm_print(f"无法播放GIF: {e}", 10000)
        g.video_playing = False
        return

    g.video_thread = Thread(target=gif_loop, daemon=True)
    g.video_thread.start()


def play_video(path, g, mgmg):
    """播放视频表情, 结束后自动回到默认表情"""

    def video_loop():
        cap = None
        try:
            cap = cv2.VideoCapture(path)
            g.video_capture = cap
            if not cap.isOpened():
                mgmg.dm_print("无法打开视频文件", 3000)
                return

            fps = cap.get(cv2.CAP_PROP_FPS) or 30
            delay = 1.0 / fps

            while _is_current(g, token) and g.video_playing:
                ret, frame = cap.read()
                if not ret:
                    break
                img = _fit_image(Image.fromarray(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)))

                def update_image(img3=img):
                    # 排队中的回调可能晚于下一次播放, 执行前必须再校验代际
                    if not _is_current(g, token):
                        return
                    g.current_photo_path = g.ImageTk.PhotoImage(img3)
                    g.emo_label.config(image=g.current_photo_path)

                mgmg.root.after(0, update_image)
                time.sleep(delay)
        except Exception as e:
            mgmg.dm_print(f"视频播放错误: {e}", 3000)
        finally:
            # 只释放自己持有的 capture, 避免误放刚接手的下一次播放的句柄
            if cap is not None:
                cap.release()
            if g.video_capture is cap:
                g.video_capture = None
            # 只有仍持有播放权时才切回默认表情, 否则会把刚接手的下一次播放打断
            if _is_current(g, token):
                g.video_playing = False

                def safe_return():
                    try:
                        if mgmg.root.winfo_exists():
                            mgmg.root.after(0, lambda: emo(g, mgmg, 0))
                    except Exception:
                        pass

                mgmg.root.after(0, safe_return)

    try:
        stop_video(g, mgmg)
        token = _next_token(g)  # 本次播放的代际: 只有它有权更新画面
        g.video_playing = True
        g.video_thread = Thread(target=video_loop, daemon=True)
        g.video_thread.start()
    except Exception as e:
        mgmg.dm_print(f"无法播放视频: {e}", 3000)
        g.video_playing = False


def emo_close(g, mgmg):
    """关闭表情显示, 清理资源"""
    _ensure_state(g)
    if g.emo_timer is not None:
        try:
            mgmg.root.after_cancel(g.emo_timer)
        except Exception:
            pass
    g.emo_timer = None
    stop_video(g, mgmg)
    if g.emo_label is not None:
        try:
            g.emo_label.pack_forget()
        except Exception:
            pass
    g.current_photo_path = None
    g.current_video_path = None


def emo(g, mgmg, number=1):
    """
    显示表情
    number: 0-5 对应 g.data_emo_path_list 的索引, 越界时回退到0
    """
    _ensure_state(g)
    try:
        if not mgmg.root.winfo_exists():
            return
    except Exception:
        return

    # 取消之前的定时器并停止当前播放
    if g.emo_timer is not None:
        try:
            mgmg.root.after_cancel(g.emo_timer)
        except Exception:
            pass
        g.emo_timer = None
    stop_video(g, mgmg)

    # 边界保护
    if number < 0 or number >= len(g.data_emo_path_list):
        number = 0

    paths = g.data_emo_path_list[number]
    if not paths:
        mgmg.dm_print(f"情绪 {number} 没有图片资源", 3000)
        g.emo_timer = mgmg.root.after(20000, lambda: emo(g, mgmg, 0))
        return

    # 确保显示 label 存在(有资源才创建, 避免空 label 挡住鼠标事件)
    if g.emo_label is None:
        try:
            g.emo_label = tk.Label(mgmg.root, bg='#123456')
            g.emo_label.pack(expand=True, fill='both')
        except Exception:
            return
    else:
        try:
            g.emo_label.pack(expand=True, fill='both')
        except Exception:
            pass
    # label 铺满主窗口, 绑定鼠标事件使点击/拖拽可用
    _bind_emo_events(mgmg, g.emo_label)

    # 避免连续重复播放同一个视频
    if len(paths) > 1 and g.current_video_path is not None:
        available = [p for p in paths if p != g.current_video_path]
        path = random.choice(available if available else paths)
    else:
        path = random.choice(paths)

    g.current_video_path = path
    ext = pathlib.Path(path).suffix.lower()

    try:
        if ext == '.gif':
            play_gif(path, 120, g, mgmg)
            g.emo_timer = mgmg.root.after(120000, lambda: emo(g, mgmg, 0))
        elif ext in ['.mp4', '.avi', '.mov', '.webm']:
            play_video(path, g, mgmg)
        else:
            img = _fit_image(Image.open(path))
            g.current_photo_path = g.ImageTk.PhotoImage(img)
            g.emo_label.config(image=g.current_photo_path)
            g.emo_timer = mgmg.root.after(120000, lambda: emo(g, mgmg, 0))
    except Exception as e:
        mgmg.dm_print(f"加载失败: emo_e {e}", 3000)
        g.emo_timer = mgmg.root.after(0, lambda: emo(g, mgmg, 0))
