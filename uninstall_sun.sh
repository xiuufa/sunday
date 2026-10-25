#!/bin/bash
# sun 卸载（便携式软件: 删除项目目录即完成彻底卸载）
#
# 做三件事:
#   1. 停止常驻 core
#   2. 清理 core 运行时文件(core/core.sock / core/core.pid / core/core.lock)
#   3. 清理旧版遗留的开机自启文件
#
# 全局 sun 命令(install_sun.sh 装的)不予清理 —— 删除项目目录后残留的无害, 由用户自行移除。
# 用法: bash uninstall_sun.sh   (删除项目目录前运行, 或直接删目录)
set -e

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
VENV_PY="$PROJECT_DIR/.venv/bin/python"
CORE_PY="$PROJECT_DIR/core.py"
CORE_DIR="$PROJECT_DIR/core"

echo "正在卸载 sun ..."

# 1. 停止 core: 直接调用客户端, 不经过 ./sun, 避免重新创建 venv
if [ -x "$VENV_PY" ] && [ -f "$CORE_PY" ]; then
    "$VENV_PY" "$CORE_PY" --client stop >/dev/null 2>&1 || true
    echo "已请求 core 停止"
else
    echo "未找到可用的 venv/core.py, 跳过 core 停止"
fi

# 2. 清理 core 运行时文件(socket / pid / lock)
for f in core.sock core.pid core.lock; do
    path="$CORE_DIR/$f"
    if [ -e "$path" ]; then
        rm -f "$path"
        echo "已清理: $path"
    fi
done

# 兼容旧版根目录运行时文件
for f in core.sock core.pid core.lock; do
    path="$PROJECT_DIR/$f"
    if [ -e "$path" ]; then
        rm -f "$path"
        echo "已清理旧版: $path"
    fi
done

rmdir "$CORE_DIR" 2>/dev/null || true

# 兼容旧版本遗留的 data/run
if [ -d "$PROJECT_DIR/data/run" ]; then
    rm -rf "$PROJECT_DIR/data/run"
    echo "已清理旧版运行时目录: $PROJECT_DIR/data/run"
fi

# 3. 旧版开机自启文件(新版 core 暂未提供自启命令, 这里仅清理遗留)
AUTOSTART_FILE="$HOME/.config/autostart/sun.desktop"
if [ -f "$AUTOSTART_FILE" ]; then
    rm -f "$AUTOSTART_FILE"
    echo "已删除旧版自启文件: $AUTOSTART_FILE"
else
    echo "未找到旧版自启文件, 跳过"
fi

echo
echo "=============================================="
echo " sun 已卸载! 删除项目目录即完成彻底卸载。"
echo " 全局 sun 命令(~/.local/bin/sun)未删除, 如需移除请执行:"
echo "   rm -f ~/.local/bin/sun"
echo "=============================================="
