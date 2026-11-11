# lobo —— 萝卜快传

局域网共享传输板。一台设备运行服务，其它手机/电脑扫码或打开网址即可加入同一块传输板；
**默认两端的角色对等**：任意一端都可以运行本服务当主机，另一端用浏览器加入。

网页与 HTTP API 与「萝卜快传」安卓端 / 桌面端完全一致（同一个可视化传输板、分片上传、Range 下载）。

## 用法

```bash
sun lobo                  # 启动 HTTP 服务并打开网页（已在运行则只打开网页）
sun lobo run              # 同 sun lobo
sun lobo get              # 打印最近一次收到的文件地址；若是文字则写入粘贴板
sun lobo send [文件路径]   # 无参数：发送粘贴板文字；有参数：发送该文件
sun lobo stop             # 停止 HTTP 服务
sun lobo status           # 运行状态与访问地址
sun lobo help             # 帮助
```

发送完成后，在另一台设备打开网页即可看到条目并下载文件；`sun lobo get` 则可以取回另一台设备发送过来的内容。

## 网页只打开一个

服务存活期间，`sun lobo` 只会打开一次网页（标记文件 `data/services/lobo.page`）：
之后再执行只打印地址、不会再新开浏览器标签；`sun lobo stop` 会清掉标记，下次启动重新打开。

## 配置（`lobo.json`）

| 字段 | 默认 | 说明 |
| --- | --- | --- |
| `port` | `8080` | 首选端口；被占用时自动在 `8080~8099` 内回退，实际端口写入 `<save_dir>/.luobo.port` |
| `save_dir` | `data/lobo` | 文件保存位置（相对项目根目录或绝对路径） |
| `host` | 空 | 监听地址，空表示程序默认（`0.0.0.0`，局域网可访问） |
| `binary` | 空 | 自定义萝卜快传可执行文件路径；空则用 `bin/` 下当前平台的程序 |
| `open_browser` | `true` | `sun lobo` / `status` 时是否自动打开网页 |
| `sender` | `sun` | 在自己传输板上显示的发送者名称 |

改完 `lobo.json` 后需要重启 core（`sun stop`，下次命令会自动拉起）。

## 内置程序

`bin/` 下是萝卜快传的单文件服务程序（由桌面端工程
`/home/xiuufa/AndroidStudioProjects/LuoboKuaichuan/desktop` 交叉编译）：

- `lobo-linux-x86_64`：Linux 静态 musl，无依赖
- `lobo-windows.exe`：Windows 便携 exe

替换/升级：重新构建桌面端后把 `desktop/dist/luobo-linux-x86_64` 与
`desktop/dist/LuoboKuaichuan.exe` 分别复制为上面两个文件名即可。

## 剪贴板

Linux 优先用 `wl-copy/wl-paste`（Wayland）或 `xclip` / `xsel`（X11）；
都没有时退回 Tk（需要图形会话，GNOME/KDE 的剪贴板管理器会在进程退出后继续保存内容）。
Windows 使用 Win32 剪贴板 API，macOS 使用 `pbcopy/pbpaste`。
