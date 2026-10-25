# Sunday

常驻 `core` + 轻量 `sun` 客户端的 Python 插件框架。

`sun` 只负责把指令转发给常驻的 `core`。模块只在 `core` 启动时初始化一次，后续每条命令都不再重复加载，省掉冷启动和重复初始化。

## 特性

- **常驻 core**：第一次 `sun` 自动拉起，之后一直运行；`sun stop` 显式停止。
- **一次性初始化**：所有模块实例化完成后，依次调用一次 `call()`；`call()` 不允许阻塞。
- **模块即目录**：`modules/<名字>/<名字>.py` 是唯一加载目标，同目录资源由模块自己管理。
- **role / name / used**：`role` 是主键，`name` 是可选别名；`used=False` 的模块可被其他模块使用，但不暴露给 `sun`。
- **同 role 替换**：同名角色的模块按 `<名字>.json` 里的 `ID` 时间戳比较，保留较新的一个。
- **统一帮助**：`sun help` 打印模块的 `desc`、默认入口和子命令。
- **模块市场**：`sun market` 负责列出、搜索、安装、启停和删除本地模块；开发者工具 `market_manager` 额外负责上传和删除云端模块。
- **共享日志**：core 通过 `G.log(message, path="")` 提供带时间戳的追加日志，默认写入 `log/sun.log`。
- **输出回传**：模块里的 `print()` 会被 core 捕获并返回给 `sun`，客户端不需要额外协议。
- **致命错误提示**：core 无法继续运行时写 `core/core.log` 并尝试弹出系统消息。
- **core 零第三方依赖**：`core.py` 只用 Python 标准库；`requirements.txt` 是插件依赖。

## 运行模型

```text
sun (短命客户端)
  → 连接 core/core.sock
  → 发送 {argv, cwd}
  ← 收到 stdout / stderr / 退出码后退出

core.py --daemon (常驻)
  → 启动时扫描并加载全部模块
  → 依次执行一次 call()
  → 阻塞监听，串行处理指令
  → 退出时清理 socket / pid / lock
```

流程：

1. `sun` 连接 `core/core.sock`。
2. 连不上就启动 `core.py --daemon`，最多等待 10 秒。
3. core 用 `core/core.lock` 保证单实例，加载模块并初始化。
4. `sun` 把参数原样发给 core；core 执行后把输出和退出码回传。
5. `sun stop` 通知 core 退出；core 清理运行时文件。

`sun` 不带参数时只确保 core 正在运行，不打印任何内容。查看帮助请用 `sun help`。

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
sun                      # 确保 core 运行；不在则拉起，在则直接返回
sun help                 # 显示用法、模块列表、模块说明和子命令
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
- `list` 不再是 core 内建命令；模块可以把它当作普通子命令使用。查看列表统一用 `sun help`。
- `sun help` 只输出全局帮助，不接受模块名参数。

## 模块市场

用户版 `market` 随项目仓库分发，只负责查询、安装和本地管理：

```bash
sun market list                 # 列出已安装模块
sun market find <关键词>        # 搜索共享 Release
sun market install <name/ID>    # 安装或更新模块
sun market mkid <name/ID>       # 生成或更新 ID
sun market used <name/ID>       # 启用模块
sun market unused <name/ID>     # 弃用模块
sun market rm <name/ID>         # 删除本地模块目录
```

开发者工具 `market_manager` 额外提供上传和删除云端：

```bash
sun market_manager upload <name/ID>   # 上传模块 zip
sun market_manager delete <name/ID>   # 删除云端模块(需确认)
```

`market_manager` 默认不随仓库分发，已加入 `.gitignore`。

市场配置在 `modules/market/market.json`：

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
- `install` 从远程下载 zip，校验后安装到 `modules/<name>/`；要替换的旧 ID 不在云端时，客户端会要求回车确认。
- `delete` 删除指定 ID 或指定 name 的全部云端版本及索引记录，执行前需要回车确认。
- `used` / `unused` 修改目标模块 JSON 的 `used` 字段。
- download 读取公开仓库，无需 token；upload / delete 需要 token：`GITEE_TOKEN` 环境变量、`env:GITEE_TOKEN`、`gitee.token`，或 `~/.config/sunday/gitee_token`。
- `find` / `install` 会把远程 `index.json` 缓存到项目根目录 `tmp/index.json`；该目录由 market 自动清理，并已加入 `.gitignore`。

## 模块开发

最小模块：

```python
class Module:
    role = "mymodule"                  # 主键，必填，非空字符串
    name = "mymodule"                  # 可选别名，省略时等于 role
    desc = "一句话说明模块作用"          # sun help 显示
    used = True                        # False: 不暴露给 sun，但仍加载并共享
    subcommands = ["status"]           # 可通过 CLI 调用的子命令

    def call(self):
        """一次性初始化：可以调整 G.MODULES_LIST 顺序"""

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
    "api": ["status"],
    "need": []
}
```

关键规则：

- 路径必须是 `modules/<目录名>/<目录名>.py`。
- 目标文件固定导出名为 `Module` 的类，构造函数必须无参。
- core 在导入前注入 `G` 和 `T`，模块里直接使用，不要继承，也不要 `from core import G`。
- `role` 必填；`name` 可选；两者都不能是 `help` / `stop`。
- `used` 可选，默认 `True`；可以写在类里或 `<名字>.json` 里，JSON 优先。设为 `False` 时模块仍会加载并可通过 `G.get()` 访问，但不会出现在 `sun help`，也不能用 `sun <role>` 调用。
- 如果模块需要 `used` 子命令（例如 `market used`），不要在类里声明 `used` 布尔属性，改用 JSON 的 `"used"`。
- `desc` 和子命令 docstring 的第一行会出现在 `sun help` 里，建议写成“作用 + 参数”。
- 需要写日志时用 `G.log("内容")`，默认追加到 `log/sun.log`；`G.log("内容", "log/other.log")` 可指定文件。
- `call()` 可选，只在 core 启动时调用一次；抛异常时模块会被移出列表。
- `call()` 可以调整 `G.MODULES_LIST`，也可以追加新模块实例。
- `call()` 不能阻塞，否则 core 无法接收命令；它按 `G.MODULES_LIST` 顺序串行执行，模块间有初始化依赖时需要自己保证顺序。
- `load()` / `start()` 不再由 core 调用，可以当作普通方法名或子命令使用。
- core 只读取 `<名字>.json` 的 `ID`、`used`、`desc`（`help` 兼容旧字段）；`api`、`need` 等字段交给 market 或模块自己使用。
- 模块只在 core 启动时初始化一次，改完代码需要重启 core。

完整规范见 [docs/插件开发手册.md](docs/插件开发手册.md)。

## 目录结构

```text
sunday/
├── core.py                 # 常驻核心 + 客户端入口
├── sun                     # Linux 启动器(首次运行自动建 .venv)
├── sun.bat                 # Windows 启动器
├── install_sun.sh          # Linux 安装: 建 .venv + 配置全局 sun
├── install_sun.ps1         # Windows 安装: 建 .venv + 配置全局 sun
├── uninstall_sun.sh        # 停止 core + 清理运行时文件
├── requirements.txt        # 插件依赖(core 本身只用标准库)
├── README.md
├── LICENSE
├── .gitignore
├── core/                   # 运行时目录(自动生成, 不入库)
│   ├── core.sock           # 客户端与 core 通信
│   ├── core.pid            # core 进程号
│   ├── core.lock           # 单实例锁
│   └── core.log            # 只记录致命错误
├── log/                    # G.log 默认输出(自动生成, 不入库)
│   └── sun.log
├── tmp/                    # 模块市场临时目录(自动清理, 不入库)
│   └── index.json
├── modules/                # 插件目录
│   ├── example/
│   │   ├── example.py
│   │   └── example.json
│   └── market/
│       ├── market.py
│       └── market.json     # Gitee 仓库、Release 配置
├── docs/
│   └── 插件开发手册.md
├── data/                   # 模块资源目录(core 不读取)
└── APP/                    # 配套 App 资源
```

> `data/` 和 `APP/` 是历史/配套资源，core 不依赖它们；是否使用由具体模块决定。
>
> `example` 会在 `call()` 里把自己从 `G.MODULES_LIST` 移除，所以默认的 `sun help` 不会显示它。`market_manager` 是开发者工具，默认不随仓库分发。

## 运行时文件

core 的运行时文件都在 `core/` 下，并且已在 `.gitignore` 中忽略：

- `core/core.sock`：Unix Domain Socket；
- `core/core.pid`：core 进程号；
- `core/core.lock`：防止重复启动；
- `core/core.log`：只记录致命错误；正常启动时可能为空。

模块日志默认在 `log/sun.log`，由 `G.log` 自动创建；`log/` 同样已在 `.gitignore` 中忽略。

core 不使用 `data/` 或 `config/`，这些目录由模块自行决定用途。

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

`sun` 不带参数只负责确保 core 运行。查看帮助请执行 `sun help`。

**`sun list` 报“未知指令”？**

`list` 已从 core 内建命令中释放，统一使用 `sun help` 查看模块列表。如果某个模块的 role/name 是 `list`，`sun list` 会调用该模块。

**模块没有出现在 `sun help` 里？**

依次检查：

1. 路径是否是 `modules/<名字>/<名字>.py`；
2. 是否导出名为 `Module` 的类；
3. `call()` 是否抛异常（失败模块会被静默移除）；
4. `call()` 是否把自身从 `G.MODULES_LIST` 移除了；
5. `used` 是否被设成了 `False`；
6. 是否修改代码后没有重启 core。

**core 起不来？**

查看 `core/core.log`。core 只对致命错误写日志并尝试弹系统消息；模块自己的业务日志用 `G.log(...)`，默认写入 `log/sun.log`。

**修改模块代码不生效？**

模块只在 core 启动时加载一次。执行 `sun stop`，下一次 `sun ...` 会自动拉起并重新初始化。

## 开源协议

本项目基于 [MIT License](LICENSE) 开源。
