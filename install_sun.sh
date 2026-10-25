#!/bin/bash
# sun 一键依赖安装脚本（绿色/便携式）
# 负责: 1. 创建 .venv + 安装依赖  2. 自动配置全局 sun 命令(可选, 卸载不清除也不影响)
# 注意: 全局命令在卸载时不清理 —— 删除项目目录后残留的无害, 用户可自行移除
# 用法: bash install_sun.sh
set -e

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
VENV_DIR="$PROJECT_DIR/.venv"

if [ ! -d "$VENV_DIR" ]; then
    echo "[sun] 首次运行, 准备虚拟环境..."

    if ! command -v python3 &> /dev/null; then
        echo "错误：未找到 python3, 请先安装 Python 3"
        exit 1
    fi

    python3 -m venv "$VENV_DIR" || {
        echo "错误: 虚拟环境创建失败, Ubuntu/Debian 请先安装: sudo apt install python3-venv"
        exit 1
    }
    "$VENV_DIR/bin/pip" install --upgrade pip -q
    if [ -f "$PROJECT_DIR/requirements.txt" ]; then
        "$VENV_DIR/bin/pip" install -r "$PROJECT_DIR/requirements.txt" -q
    fi
    echo "[sun] 虚拟环境就绪"
else
    echo "[sun] .venv 已存在, 跳过"
fi

# 2. 配置全局 sun 命令: 写入用户 bin 目录 + 注入 PATH(缺 PATH 才注入, 幂等)
INSTALL_DIR="$HOME/.local/bin"
mkdir -p "$INSTALL_DIR"
cat > "$INSTALL_DIR/sun" <<EOF
#!/bin/bash
# sun 全局启动器 (由 install_sun.sh 生成, 修改项目路径请重跑安装脚本)
PROJECT_DIR="$PROJECT_DIR"
exec "\$PROJECT_DIR/sun" "\$@"
EOF
chmod +x "$INSTALL_DIR/sun"
echo "[sun] 已安装全局命令: $INSTALL_DIR/sun$([ "$INSTALL_DIR" = "/usr/bin" ] && echo " (系统目录)" || echo " (用户目录)")"

# 将用户 bin 目录加入 PATH(仅在缺省时追加一次, 幂等)
MARK="# sun: 将用户 bin 目录加入 PATH"
rc_found=0
rc_added=0
for RC in "$HOME/.bashrc" "$HOME/.zshrc"; do
    [ -f "$RC" ] || continue
    rc_found=1
    if ! grep -qs "sun: 将用户 bin 目录加入 PATH" "$RC"; then
        printf '%s\nexport PATH="%s:$PATH"\n' "$MARK" "$INSTALL_DIR" >> "$RC"
        rc_added=1
        echo "[sun] 已向 $RC 注入 PATH"
        break
    fi
done
if [ "$rc_added" -eq 0 ]; then
    if [ "$rc_found" -eq 1 ]; then
        echo "[sun] 全局命令 PATH 已配置过, 跳过"
    else
        echo "[sun] 未找到 ~/.bashrc 或 ~/.zshrc, 请手动把 $INSTALL_DIR 加入 PATH"
    fi
fi

echo
echo "=============================================="
echo " 依赖与全局命令配置完成!"
echo "  运行   : 任意目录执行  sun  或  sun help"
echo "  全局命令已自动配置, 新终端或 source 后生效"
echo "  core   : 首次运行 sun 时自动拉起"
echo "  停止   : sun stop"
echo "=============================================="
