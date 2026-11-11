# Sunday

常驻 `core` + 轻量 `sun` 客户端的 Python 插件框架。

`sun` 只负责把指令转发给常驻的 `core`。core 启动时只挂载 `log` / `init` / `server` 三件套，再按 `core/init.json` 初始化需要的模块；其余模块在第一次被调用时按需加载，不自动初始化。

## 特性

- **常驻 core**：第一次 `sun` 自动拉起，之后一直运行；`sun stop` 显式停止。
- **按需初始化**：`core/init.json` 是模块列表（`used` 和顺序）；模块是否随 core 启动初始化，由同目录 `<名字>.json` 的 `init` 决定。`used && init` 的模块在启动时初始化，其余模块第一次被 `sun` 调用时只加载、不初始化。`sun init [模块...]` 可手动初始化。`market` 安装、启用、删除模块时会自动维护 `core/init.json`。
- **模块即目录**：`modules/<名字>/<名字>.py` 是唯一加载目标，同目录资源由模块自己管理。
- **role / name / used**：`role` 是主键，`name` 是可选别名；`used=False` 的模块不暴露给 `sun`，也不会被初始化。
- **同 role 替换**：同名角色的模块按 `<名字>.json` 里的 `ID` 时间戳比较，保留较新的一个。
- **统一帮助**：`sun help` 打印每个模块的 `role / name` 和 `desc`；`sun list` 只打印各模块的 `name`；参数、子命令等细节由模块自己的 `help` 子命令提供。
- **模块市场**：`market`（发布版，面向用户）和 `market_manager`（开发者工具，通过 Release 分发）共用 `role = market`，按 `ID` 时间戳只加载一个；负责列出、搜索全部云端模块、依赖检查、安装、启用/弃用和删除本地模块，`market_manager` 额外负责上传和删除云端模块。
- **vision**：ADB 投屏 + easyocr 屏幕文字识别，按优先级点击「领取奖励」等文字；识别循环由 `G.server` 托管，不阻塞 core。
- **共享日志**：core 通过 `G.log(message, path="")` 提供带时间戳的追加日志，默认写入 `log/sun.log`。
- **输出回传**：模块里的 `print()` 会被 core 捕获并返回给 `sun`，客户端不需要额外协议。
- **致命错误提示**：core 无法继续运行时写 `core/core.log` 并尝试弹出系统消息。
- **core 零第三方依赖**：`core/core.py` 只用 Python 标准库；`requirements.txt` 是插件依赖。

## 运行模型

```text
sun (短命客户端)
  → 连接 core/core.sock
  → 发送 {argv, cwd}
  ← 收到 stdout / stderr / 退出码后退出

core/core.py --daemon (常驻)
  → G / T 不存在就创建
  → 挂载 log / init / server 三件套
  → init 读 core/init.json 与模块 JSON, 初始化 used && init 的模块
     初始化 = G.<模块> = 实例 + 调用模块的 init()
  → 阻塞监听，串行处理指令
  → 退出或收到信号: init.shutdown() + server.shutdown()
```

流程：

1. `sun` 连接 `core/core.sock`。
2. 连不上就启动 `core/core.py --daemon`，最多等待 10 秒。
3. core 用 `core/core.lock` 保证单实例，挂载三件套并按 `core/init.json` 初始化。
4. `sun` 把参数原样发给 core；目标模块未初始化时只按需加载并直接执行，不自动补初始化。
5. `sun stop` 通知 core 退出；core 调用 init / server 的关停逻辑并清理运行时文件。

`sun` 不带参数时确保 core 正在运行；如果 core 已在运行，会先停止再重新拉起。不打印任何内容，查看帮助请用 `sun help`。

## 环境要求

- Python 3.10+
- Linux 为当前主要开发和验证环境
- Linux 创建虚拟环境需要 `python3-venv`：

```bash
sudo apt install python3-venv python3-tk
```

- Windows 提供 `sun.bat` 和 `install_sun.ps1`，但 core 使用 Unix Domain Socket，实际可用性取决于目标系统和 Python 对 `AF_UNIX` 的支持。
- `requirements.txt` 是插件依赖（Pillow、OpenCV、easyocr 等），首次安装体积较大；只运行 core 不需要这些包。

## 安装

### Linux

```bash
./install_sun.sh
```

安装脚本会：

1. 创建项目内的 `.venv`；
2. 安装 `requirements.txt`；
3. 生成全局命令 `~/.local/bin/sun`；
4. 在需要时把 `~/.local/bin` 加入 `~/.bashrc` / `~/.zshrc` 的 `PATH`。

没有安装全局命令也可以直接使用项目内的启动器：

```bash
./sun
./sun help
```

首次运行 `./sun` 也会自动创建 `.venv` 并安装依赖。

### Windows

```powershell
powershell -ExecutionPolicy Bypass -File install_sun.ps1
```

脚本会创建 `.venv`、安装依赖，并生成 `%USERPROFILE%\.bin\sun.cmd`。新开终端后即可使用 `sun`。

## 使用

```bash
sun                      # 确保 core 运行；已在运行则重启
sun help                 # 显示用法和模块列表（role / name - desc）
sun list                 # 只列出各模块 name
sun init [模块...]       # 按模块 JSON 的 init 初始化, 或初始化指定模块
sun <role>               # 调用模块的 run()
sun <role> <子命令> ...   # 调用 subcommands 中登记的方法
sun stop                 # 停止 core
```

示例：

```bash
sun help
sun mymodule
sun mymodule status
sun mymodule status a b
sun stop
```

说明：

- `run()` 只在没有子命令时调用，不要把子命令解析堆在 `run()` 里。
- 子命令必须登记在模块的 `subcommands` 列表里，且对应同名方法。
- `name` 别名和 `role` 都可以用来调用模块。
- `sun list` 是 core 内建命令，只打印各模块的 `name`；模块不能再占用 `list` 这个 role/name。
- `sun help` 只输出全局帮助，不接受模块名参数。
- 查看某个模块的详细说明用 `sun <模块> help`（模块提供 `help` 子命令时，例如 `sun market help`、`sun whiteblob help`）。

vision（需要设备开启 USB 调试，以及 `adb` / `ffmpeg` / `easyocr`）：

```bash
sun vision devices                 # 列出在线设备
sun vision shot                    # 截一帧到 data/vision/，用于量坐标
sun vision run region=0,100,1260,2300 close_btn=1144,200
sun vision status                  # 运行状态与 MJPEG 投屏地址
sun vision stop
```

`run` 的坐标会保存到 `data/vision/config.json`，之后可直接 `sun vision run`；运行日志在 `core/services/vision.log`。

## 模块市场

市场有两个模块，`role` 都是 `market`；同时存在时按 `ID` 时间戳只加载一个：

- `market`：面向用户的发布版，随项目一起分发；没有云端上传和删除权限；
- `market_manager`：开发者工具，通过 Release 分发，额外支持上传和删除云端模块。

两者的 `list / find / find_all / auto / install / mkid / used / unused / rm` 完全一致，只是 `market` 不含 `upload / delete`。

本地不必同时保留两个；`role` 相同，所以 `sun market ...` 和 `sun market_manager ...` 都会命中当前加载的那个。只想验证发布版时，可把 `modules/market_manager` 目录临时改名（如 `_market_manager`），core 会跳过 `_` 开头的目录；验证完改回并重启 core。

```bash
sun market_manager list                 # 列出已安装模块
sun market_manager find <关键词>        # 搜索共享 Release
sun market_manager find_all             # 列出共享 Release 中所有模块
sun market_manager auto                 # 检查依赖并整理初始化顺序
sun market_manager install <name/ID>    # 安装或更新模块
sun market_manager mkid <name/ID>       # 生成或更新 ID
sun market_manager used <name/ID>       # 启用模块
sun market_manager unused <name/ID>     # 弃用模块
sun market_manager rm <name/ID>         # 删除本地模块目录
sun market_manager upload <name/ID>     # 上传模块 zip(仅 market_manager)
sun market_manager delete <name/ID>     # 删除云端模块(仅 market_manager, 需确认)
```

只想读代码、不想启用某个模块：执行 `sun market unused <name>`，它会把模块 JSON 和 `core/init.json` 里的 `used` 一起设为 `false`。模块不出现在 `sun help`，也不能用 `sun <role>` 调用。

市场配置：

- 开发本仓库：先安装 `market_manager`，其配置在 `modules/market_manager/market_manager.json`；
- 面向用户的发布版：`modules/market/market.json`。

发布版只需要：

```json
{
    "gitee": {
        "repo": "xiuufa/sunday",
        "release_tag": "mods",
        "index_asset": "index.json"
    }
}
```

`market_manager.json` 额外支持 `token`、`release_name`、`target_commitish`：

```json
{
    "gitee": {
        "repo": "xiuufa/sunday",
        "token": "",
        "release_tag": "mods",
        "release_name": "Sunday modules",
        "target_commitish": "master",
        "index_asset": "index.json"
    }
}
```

- 所有模块共用一个 Gitee Release，tag 为 `mods`。
- 每个版本保存为 `<ID>.zip`，不再删除旧版本。
- `index.json` 记录所有版本；`find` 按 `name`、`role`、`ID`、`desc` 匹配，同 `name` 只显示最新。
- 同一个 ID 不允许重复上传；需要更新时先执行 `sun market mkid <name>`。
- `find_all` 不按关键词过滤，直接列出 `index.json` 中的全部模块；同样支持缓存回退。
- `auto` 记录所有本地模块的 `role`，按各模块 JSON 的 `依赖` 检查 Python 包（缺失时询问是否 `pip install`），并把已安装的 `依赖模块` 在 `core/init.json` 中移到依赖它的模块前面；未安装时只提示 `sun market find <role>`。
- `install` 从远程下载 zip，校验后安装到 `modules/<name>/`；要替换的旧 ID 不在云端时，客户端会要求输入 `y` 确认。安装完成后会自动执行一次 `auto`（检查依赖并整理初始化顺序）。
- `delete` 删除指定 ID 或指定 name 的全部云端版本及索引记录，执行前需要输入 `y` 确认。
- 所有二次确认都是输入 `y` 继续，直接回车取消。
- `used` / `unused` 修改目标模块 JSON 的 `used` 字段，并同步 `core/init.json`。
- download 读取公开仓库，无需 token；upload / delete 需要 token：`GITEE_TOKEN` 环境变量、`env:GITEE_TOKEN`、`gitee.token`，或 `~/.config/sunday/gitee_token`。
- `find` / `install` 会把远程 `index.json` 缓存到项目根目录 `tmp/index.json`；该目录由市场模块启动时清空，并已加入 `.gitignore`。

## 模块开发

最小模块：

```python
class Module:
    role = "mymodule"                  # 主键，必填，非空字符串
    name = "mymodule"                  # 可选别名，省略时等于 role
    desc = "一句话说明模块作用"          # sun help 显示
    used = True                        # False: 不暴露给 sun，但仍加载并共享
    subcommands = ["status"]           # 可通过 CLI 调用的子命令

    def init(self):
        """可选：core 初始化时调用；不能阻塞"""

    def shutdown(self):
        """可选：core 退出时清理"""

    def run(self):
        """默认入口：无子命令时执行"""
        print("mymodule run")

    def status(self):
        """查看状态；用法: sun mymodule status [--json]"""
        print("ok")
```

同目录的 `mymodule.json`：

```json
{
    "name": "mymodule",
    "role": "mymodule",
    "desc": "一句话说明模块作用",
    "ID": "mymodule1792906494",
    "used": true,
    "init": false,
    "依赖": [],
    "依赖模块": []
}
```

关键规则：

- 路径必须是 `modules/<目录名>/<目录名>.py`。
- 目标文件固定导出名为 `Module` 的类，构造函数必须无参。
- core 在导入前注入 `G` 和 `T`，模块里直接使用，不要继承，也不要 `from core import G`。
- `init()` 可选，只在 core 启动且模块 JSON 的 `init=true`、`core/init.json` 中 `used=true` 时调用；必须快速返回，不能阻塞。初始化 = `G.<模块> = 实例` + `模块.init()`。进程类模块（`lobo`、`whiteblob`）在 `__init__` 挂载时完成 `G.server.register`，在 `init()` 里直接调用 `run()` 启动服务。
- `shutdown()` 可选，core 退出或收到 SIGINT / SIGTERM / SIGHUP 时调用。
- `role` 必填，是主键；`name` 只是可选别名。`role` 是 `help` / `stop` / `list` / `init` 时模块会被丢弃；`name` 是保留字时会自动退回 `role`。
- `used` 可以写在类里、`<名字>.json` 里或 `core/init.json` 里；`init.json` 优先。`used=False` 的模块不开放给 `sun`，也不出现在 `sun help`。
- `init` 写在 `<名字>.json` 里，决定模块是否随 core 启动初始化；`market` 安装模块时会把该值写入 `core/init.json`。当前只有 `lobo` 和 `whiteblob` 为 `true`。
- 如果模块需要 `used` 子命令（例如 `market used`），不要在类里声明 `used` 布尔属性，改用 JSON 的 `"used"`。
- `desc` 会出现在 `sun help` 里；core 不解析 docstring、不列子命令，参数和子命令说明写进模块自己的 `help` 子命令（参考 `whiteblob`、`market_manager`）。
- 需要写日志时用 `G.log("内容")`，默认追加到 `log/sun.log`；`G.log("内容", "log/other.log")` 可指定文件。
- `sun <模块> <子命令>` 不会自动补初始化：模块未初始化时只按需导入并直接执行；如果 `init.json` 要求它必须初始化，则提示 `模块 X 未初始化`。
- `G.init.load()` / `save(entries)` / `ensure(token)` / `set(token, used, init)` / `remove(token)` 用于读写 `core/init.json`。
- `G.call("lobo", "get")` 可在模块内执行一条 sun 指令，返回 `(code, stdout, stderr)`，与 `sun lobo get` 同一套路由，不启动子进程。
- `G.server` 由 core 内置的 `server` 工具提供，始终可用。需要常驻服务时用 `G.server.register(name, argv, marker="", env=None, persist=False)` 登记；`G.server.wait(name, timeout, probe)` 等待服务就绪；`persist=True` 的服务在 core 退出时保留（默认随 core 停止）。参考 `whiteblob` 的 facade + `serve.py` 写法。
- GUI 工具面板按钮由 `whiteblob` 提供：在模块 `init()` 里调用 `G.get("gui").add_button(text, command)` 注册，`command` 是不带 `sun` 的命令（如 `"lobo get"`）；失败用 `G.log` 记录后继续。参考 `lobo`。
- `load()` / `start()` 不再有生命周期含义，可以当作普通方法名或子命令使用。
- core 只读取 `<名字>.json` 的 `ID`、`used`、`init`、`desc`（`help` 兼容旧字段）；`依赖`、`依赖模块`、`api` 不由 core 使用。`ID` 优先读 JSON，没有时用类属性 `ID` 兜底。
- `依赖` 写 python 包（同 `requirements.txt` 写法），`依赖模块` 写需要的模块 role；`sun market auto`（`market_manager` 同样可用）会检查/安装 python 依赖，并把已安装的依赖模块在 `core/init.json` 中排到前面。
- 改完模块代码需要重启 core（`sun stop` 后下一次 `sun` 会重新拉起）。
- `modules/example` 只是结构和接口示例：`used = False` 且在 `init()` 里把自己移出列表，所以默认 `sun help` 不显示、也不能运行；想运行就删掉移除那行并把 `used` 改 `True`。

完整规范见 [docs/插件开发手册.md](docs/插件开发手册.md)。

## 目录结构

```text
sunday/
├── core/                   # 常驻核心目录
│   ├── core.py             # core 入口: 加载模块、监听 UDS、客户端转发
│   ├── init.py             # 工具: 按 init.json 初始化模块
│   ├── init.json           # 模块列表: name / used / init(模块可写)
│   ├── log.py              # 工具: 日志写入
│   ├── server.py           # 工具: 常驻进程管理
│   ├── core.sock           # 运行时: 客户端与 core 通信(自动生成)
│   ├── core.pid            # 运行时: core 进程号(自动生成)
│   ├── core.lock           # 运行时: 单实例锁(自动生成)
│   └── core.log            # 运行时: 致命错误(自动生成)
├── sun                     # Linux 启动器(首次运行自动建 .venv)
├── sun.bat                 # Windows 启动器
├── install_sun.sh          # Linux 安装: 建 .venv + 配置全局 sun
├── install_sun.ps1         # Windows 安装: 建 .venv + 配置全局 sun
├── uninstall_sun.sh        # 停止 core + 清理运行时文件
├── requirements.txt        # 插件依赖(core 本身只用标准库)
├── README.md
├── LICENSE
├── .gitignore
├── log/                    # G.log 默认输出(自动生成, 不入库)
│   └── sun.log
├── tmp/                    # 模块市场临时目录(自动清理, 不入库)
│   └── index.json
├── modules/                # 插件目录
│   ├── example/            # 结构/接口示例(used=false，不参与 sun)
│   ├── lobo/               # role = lobo，萝卜快传局域网传输板
│   │   ├── lobo.py         # facade: 启停服务、收发文件/文字
│   │   ├── clipboard.py    # 跨平台剪贴板
│   │   └── bin/            # 内置萝卜快传单文件服务程序
│   ├── whiteblob/          # role = gui
│   │   ├── whiteblob.py    # facade: 挂载时登记、init() 启动、命令代理
│   │   ├── serve.py        # 独立窗口进程(python -m modules.whiteblob.serve)
│   │   └── common.py       # 共享常量
│   ├── vision/             # role = vision，ADB 投屏 + OCR 自动化
│   │   ├── vision.py       # facade: run/stop/status/shot/devices
│   │   ├── worker.py       # 后台识别循环(core 托管)
│   │   ├── common.py       # 常量、坐标配置与参数解析
│   │   └── tools/          # adb.py 设备操作 / mirror.py 投屏帧流
│   └── market/             # role = market，用户发布版
├── docs/
│   └── 插件开发手册.md
├── data/                   # 模块资源目录(core 不读取)
└── APP/                    # 配套 App 资源
```

> `data/` 和 `APP/` 是历史/配套资源，core 不依赖它们；是否使用由具体模块决定。
>
> `example` 会在 `init()` 里把自己从 `G.MODULES_LIST` 移除，所以默认的 `sun help` 不会显示它；它只是结构/接口示例。`market_manager` 是开发者工具，通过 Release 分发；面向用户的发布版用 `market`（`role` 也是 `market`）。
>
> `init` / `server` / `log` 都是 `core/` 里的内置工具，不是插件：`init` 读取 `core/init.json` 的列表并结合模块 JSON 的 `init` 决定初始化哪些模块，`server` 提供 `G.server`，`log` 支撑 `G.log`。

## 运行时文件

core 的运行时文件都在 `core/` 下，并且已在 `.gitignore` 中忽略：

- `core/core.sock`：Unix Domain Socket；
- `core/core.pid`：core 进程号；
- `core/core.lock`：防止重复启动；
- `core/core.log`：core 自身的致命错误；daemon 的 stdout/stderr 也重定向到这里，所以模块导入和初始化输出也会落在这里。
- `core/services/`：`server` 工具托管的常驻进程 PID 与日志。

模块日志默认在 `log/sun.log`，由 `G.log` 自动创建；`log/` 同样已在 `.gitignore` 中忽略。

core 不使用 `data/`，该目录由模块自行决定用途。

## 卸载

```bash
bash uninstall_sun.sh
rm -rf /path/to/sunday
```

`uninstall_sun.sh` 会停止 core 并清理 `core/` 下的 socket、pid、lock 文件。全局命令不会自动删除，需要时手动移除：

```bash
rm -f ~/.local/bin/sun
```

Windows 则删除 `%USERPROFILE%\.bin\sun.cmd`，并从用户 `PATH` 中移除对应目录。

## 常见问题

**`sun` 没有输出？**

`sun` 不带参数负责确保 core 运行；已在运行时先停止再重新拉起。查看帮助请执行 `sun help`。

**`sun list` 和 `sun help` 有什么区别？**

`sun list` 只打印各模块的 `name`；`sun help` 还会打印用法说明。`list` 现在是 core 内建命令，不接受参数。

**模块没有出现在 `sun help` 里？**

依次检查：

1. 路径是否是 `modules/<名字>/<名字>.py`；
2. 是否导出名为 `Module` 的类；
3. `init()` 是否抛异常（初始化失败会写入 `G.log`）；
4. `init()` 是否把自身从 `G.MODULES_LIST` 移除了；
5. `used` 是否在模块 JSON 或 `core/init.json` 里被设成了 `False`；
6. 是否修改代码后没有重启 core。

**core 起不来？**

查看 `core/core.log`。core 的致命错误、daemon 的 stdout/stderr（包括模块导入和初始化的输出）都会写到这里；模块自己的业务日志用 `G.log(...)`，默认写入 `log/sun.log`。

**修改模块代码不生效？**

模块在 core 进程内只导入一次，`init()` 只由 core 启动（模块 JSON `init=true`）或 `sun init` 触发。执行 `sun stop`，下一次 `sun ...` 会自动拉起并使用新代码。

**提示“模块 X 未初始化”？**

该模块在 `core/init.json` 里是 `used=true, init=true`，但当前 core 没有成功初始化它。执行 `sun init X`，或修正初始化报错后重启 core。

## 开源协议

本项目基于 [MIT License](LICENSE) 开源。
