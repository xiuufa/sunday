# Sunday — 模块化桌面框架

一套 **GUI + CLI 双栖**、可插拔的桌面应用框架。它不只是一只宠物：它把每个"想加速的想法"做成一个模块，谁都能往里新建脚本、实现工具、改造窗口组件。

它不做死任何形态。你想让它是个桌宠、是个快捷启动器、是个小工具台、还是某个复杂脚本的展示壳，全由你写的模块决定。

- **GUI**：默认用 `mgmg.py`（tkinter）做桌宠主体，气泡、剪贴板、工具面板开箱即用；**正在开发 Qt 版本**（更稳的透明背景与键盘事件）。
- **CLI**：同一个模块两种入口——`sun <模块> run` 命令行调用，GUI 右键工具面板也能一键触发。

## 设计理念

- **约定优于配置**：`mudels/*.py` 即一个模块，放进目录自动加载，构造参数注入全局对象 `g`，模块之间通过 `self.g.xxx` 互相访问——新增一个能力 = 新增一个文件。
- **GUI 与逻辑解耦**：模块内同一个方法既能被 CLI 调用，也能被 GUI 按钮触发；全程不依赖具体窗口环境，CLI 下照常工作。
- **重载覆盖**：想改任何现成组件，不用改框架源码，插件里直接对 `self.g` 上的目标模块做覆盖即可。

## 特性

- **模块化插件架构**：`mudels/*.py` 即一个模块，自动加载、自动注入全局对象 `g`
- **统一 CLI**：`sun <模块> [子命令] [参数...]` 一行调用任意模块
- **命令中枢**：`sun list` 按调用次数降序列出所有可用命令
- **后台运行**：`sun` 启动桌宠后立即释放终端，支持 PID 管理、命令行关闭
- **开机自启**：`sun --autostart on|off` 显式管理；`sun --auto` 为登录入口，配合 `mgmg_auto_start` 配置开关
- **插件重载**：插件可通过 `self.g` 对其它模块（含 mgmg 组件）做"重载覆盖"，实现你想改的所有组件

## 环境要求

- Linux / Ubuntu（首选，开发环境）
- Windows（已适配：透明背景/开机自启/全局命令均可用）
- Python 3.10+
- 虚拟环境 `.venv`（含 tkinterdnd2、Pillow、opencv-python、imageio、requests）

## 安装

项目为便携式形态：不注册系统、不写服务，**删除项目目录即完成彻底卸载**。`install_sun.sh` 只做两件事——建 `.venv` 装依赖 + 自动配置全局 `sun` 命令（卸载时不清除全局命令，残留无害）。

```bash
./install_sun.sh
```

装完后 **全局可用 `sun`**（任意目录，新终端或 `source ~/.bashrc` 后生效）：

```bash
sun              # 启动桌宠(后台运行, 立即返回终端)
sun list         # 列出所有可调用命令
```

> 若不想装全局命令，直接用项目内启动器也行：`./sun` / `./sun list`。

### Windows 安装

Windows 用 PowerShell 安装脚本，等价 Linux 的 `install_sun.sh`（建 venv + 配置全局 `sun` 命令）：

```powershell
powershell -ExecutionPolicy Bypass -File install_sun.ps1
```

装完后新开一个终端即可全局使用 `sun`（需 `sun.bat` 所在目录，PowerShell 会写入用户 PATH）；不想装全局就用 `sun.bat` 本地启动。

## 开机自启（可选）

登录后自动启动桌宠，显式开启/关闭：

```bash
sun --autostart on     # 开启(Linux 写 ~/.config/autostart, Windows 写启动文件夹)
sun --autostart off    # 关闭
```

开启后登录会经 `sun --auto` 拉起桌宠；是否真正启动由配置 `mgmg_auto_start` 控制。

## 卸载

删除项目目录即完成卸载。若开过开机自启，先关闭：

```bash
sun --autostart off
rm -rf ~/py/sunday        # 删除整个项目目录
```

`uninstall_sun.sh` 会帮你清理开机自启文件。全局 `sun` 命令（`~/.local/bin/sun`）不随卸载删除，如需移除手动执行 `rm -f ~/.local/bin/sun`。

## 使用

> 安装后全局可用 `sun`；未装全局则用项目内 `./sun`，等价。

```bash
sun                       # 启动桌宠(后台运行, 立即返回终端)
sun --auto                # 开机自启入口(按 mgmg_auto_start 配置决定是否启动)
sun list                  # 列出所有可调用命令(按调用次数降序)
sun mgmg list             # 列出 mgmg 的可调用子命令
sun mgmg run              # 打开桌宠 GUI(并设置下次自动启动)
sun mgmg stop             # 关闭后台桌宠(并禁止下次自动启动)
sun dzbc                  # 调用 mudels/dzbc.py 的 run() → 用默认浏览器打开豆包
sun lobo run              # 启动手机互联服务(与配套 Android App 通信)
sun lobo ./a.png          # 把文件发送给已连接的手机 App
sun <模块> <参数...>       # 调用任意模块的 run()
```

### 桌宠操作

| 操作 | 行为 |
| --- | --- |
| 左键单击 | 展开并显示表情 |
| 左键双击 | 正在考虑实现chat功能 |
| 左键三击 | 最小化 |
| 右键 | 打开工具面板（工具 + 剪贴板历史 + 关于/关闭） |
| 拖拽 | 移动桌宠 |
| 拖放文件/文本 | 展开桌宠并显示开心表情 |
| 右键 → 剪贴板 | 读取当前剪贴板并生成一个"〇"按钮（悬停预览 / 左键写回 / 右键删除） |

## 模块

| 模块 | 说明 |
| --- | --- |
| `mgmg` | 默认 GUI 桌宠：窗口/气泡/鼠标事件/工具面板 |
| `emo` | 表情渲染（图片/GIF/视频），无类函数模块 |
| `clipboard` | 剪贴板插件：历史、快捷键、重载 mgmg 组件 |
| `dzbc` | 打开 doubao.com（CLI + GUI 工具面板） |
| `kimi` | 打开 kimi.com（CLI + GUI 工具面板） |
| `lobo` | 与配套 App（lobo）通信：剪贴板互传、文件收发（TCP/UDP） |
| `config` | JSON 配置读写（`data/config/config.json`） |
| `log` | 日志写入（`log/log.txt`，带文件锁） |
| `cmd` | 命令中枢：调用统计 + `sun list` |

## 目录结构

```
sunday/
├── sun_cli.py            # CLI 入口(薄调度层): 加载模块/分发命令/后台化
├── sun                   # Linux 启动器(自动建 .venv)
├── sun.bat               # Windows 启动器(自动建 .venv)
├── install_sun.sh        # Linux 安装: 建 .venv + 配置全局 sun 命令
├── install_sun.ps1       # Windows 安装: 建 .venv + 配置全局 sun 命令
├── uninstall_sun.sh      # 卸载残留清理(开机自启文件)
├── LICENSE               # MIT 开源协议
├── .gitignore            # 忽略运行时产物(.venv / log / config.json 等)
├── mudels/               # 模块目录(每个 .py 是一个插件)
│   ├── base.py           # BaseModule 基类
│   ├── cmd.py            # 命令中枢(统计 + list)
│   └── ...               # 各功能模块
├── data/
│   ├── config/config.json  # 配置文件(首次运行自动生成, 不入库)
│   └── emo/0..5/          # 表情资源目录
└── docs/插件开发手册.md    # 插件开发规范
```

## 开发

插件开发请阅读 [docs/插件开发手册.md](docs/插件开发手册.md)。

核心机制一句话：**`mudels/` 下的每个 `.py` 文件会被自动加载并实例化，构造参数注入全局对象 `g`（各模块通过 `self.g.xxx` 互相访问）**。

## 开源协议

本项目基于 [MIT License](LICENSE) 开源。

## 进度说明

### 配套框架
本项目已基本完成基于python的框架开发,并配备有与lobo(文件,text传输)配套APP(分发版APK在./APP目录下,正在持续改善中)

lobo APP简介：

一个实现与PC脚本进行通信的APP壳，后续将开源代码进一步开放

想了又想,lobo和微信等传输方式的区别是没有限制,速度也会快一点,后续可能会支持更多功能如AI chat

### GUI问题
目前基础版GUI模块（mgmg）基于Python的 tkinter 开发。

由于Windows与Ubuntu系统的兼容有所差距，因而舍弃了按键绑定（Windows系统没有问题，可自行实现）等重要功能实现，且Ubuntu系统中对GUI透明组件的支持欠佳，造成异常蓝色背景

在后续版本中将GUI实现的依赖积极迁移为Qt库，将消解以上问题。
