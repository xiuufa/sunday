@echo off
setlocal
rem 记录调用者目录, 供 core 常驻进程读取
set "SUN_CLIENT_CWD=%CD%"

rem 进入项目目录, 保证模块内的相对路径(如 .\data)有效
cd /d "%~dp0"

set "VENV_DIR=%CD%\.venv"
set "CLI_SCRIPT=%CD%\core.py"

rem 首次运行: 自动创建 venv 并安装依赖
if not exist "%VENV_DIR%\Scripts\python.exe" (
    echo [sun] 首次运行, 准备虚拟环境...
    where python >nul 2>nul
    if errorlevel 1 (
        echo 错误: 未找到 python, 请先安装 Python 3
        exit /b 1
    )
    python -m venv "%VENV_DIR%"
    if errorlevel 1 (
        echo 错误: 虚拟环境创建失败, 请检查 Python 是否完整安装
        exit /b 1
    )
    "%VENV_DIR%\Scripts\python.exe" -m pip install --upgrade pip -q
    if exist "%CD%\requirements.txt" (
        "%VENV_DIR%\Scripts\python.exe" -m pip install -r "%CD%\requirements.txt" -q
    )
    echo [sun] 虚拟环境就绪
)

rem 用 venv 的 Python 执行 core 客户端, 透传所有参数
"%VENV_DIR%\Scripts\python.exe" "%CLI_SCRIPT%" --client %*
