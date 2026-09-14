#!/bin/bash
# sun 卸载（便携式软件: 删除项目目录即完成卸载）
#
# 只做一件事: 清理 self 的辅助性残留(开机自启文件)。
# 全局 sun 命令(install_sun.sh 装的)不予清理 —— 删除项目目录后残留的无害, 由用户自行移除。
# 用法: bash uninstall_sun.sh   (删除项目目录前运行, 或直接删目录)
set -e

echo "正在卸载 sun ..."

# 开机自启配置(install_sun.sh / --autostart on 创建的)
AUTOSTART_FILE="$HOME/.config/autostart/sun.desktop"
if [ -f "$AUTOSTART_FILE" ]; then
    rm -f "$AUTOSTART_FILE"
    echo "已删除: $AUTOSTART_FILE"
else
    echo "未找到 $AUTOSTART_FILE, 跳过"
fi

echo
echo "=============================================="
echo " sun 已卸载! 删除项目目录即完成彻底卸载。"
echo " 全局 sun 命令(~/.local/bin/sun)未删除, 如需移除请执行:"
echo "   rm -f ~/.local/bin/sun"
echo "=============================================="
