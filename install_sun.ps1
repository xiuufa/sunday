# install_sun.ps1 - Windows 一键安装全局 sun 命令
# 用途: 生成全局 sun 命令 wrapper + 追加进用户 PATH(幂等), 用法等价 Linux 的 install_sun.sh
# 说明: 不清理自启(自启由 sun --autostart 管理)、卸载时也不删除全局命令
# 用法: powershell -ExecutionPolicy Bypass -File install_sun.ps1
$ErrorActionPreference = "Stop"

$ProjectDir = (Get-Location).Path
$VenvPy     = Join-Path $ProjectDir ".venv\Scripts\python.exe"

# 0. 首次运行先建 venv + 装依赖(复用 sun.bat 的引导逻辑)
if (-not (Test-Path $VenvPy)) {
    Write-Host "[sun] 首次运行, 准备虚拟环境..."
    if (-not (Get-Command python -ErrorAction SilentlyContinue)) {
        Write-Host "[sun] 错误: 未找到 python, 请先安装 Python 3(https://www.python.org/), 安装时勾选 'Add to PATH'"
        exit 1
    }
    python -m venv (Join-Path $ProjectDir ".venv")
    if (-not (Test-Path $VenvPy)) {
        Write-Host "[sun] 错误: 虚拟环境创建失败, 请重新安装 Python 并勾选 'Add python.exe to PATH'"
        exit 1
    }
    & $VenvPy -m pip install --upgrade pip -q
    $Req = Join-Path $ProjectDir "requirements.txt"
    if (Test-Path $Req) {
        & $VenvPy -m pip install -r $Req -q
    }
    Write-Host "[sun] 虚拟环境就绪"
}
else {
    Write-Host "[sun] .venv 已存在, 跳过"
}

# 1. 生成全局 wrapper: %USERPROFILE%\.bin\sun.cmd, 转发到 sun.bat
$BinDir   = Join-Path $env:USERPROFILE ".bin"
$SunCmd   = Join-Path $BinDir "sun.cmd"
New-Item -ItemType Directory -Force -Path $BinDir | Out-Null
$BatPath  = Join-Path $ProjectDir "sun.bat"
$Content  = "@echo off`r`n`"$BatPath`" %*"
[System.IO.File]::WriteAllText($SunCmd, $Content, [System.Text.Encoding]::ASCII)
Write-Host "[sun] 已生成全局命令: $SunCmd"

# 2. 把 .bin 加进用户 PATH(注册表 HKCU\Environment), 幂等, 不重复
$EnvPathKey = "HKCU:\Environment"
$UserPath = (Get-ItemProperty -Path $EnvPathKey -Name Path -ErrorAction SilentlyContinue).Path
if ($UserPath -and ($UserPath -split ';' -contains $BinDir)) {
    Write-Host "[sun] PATH 已包含 $BinDir, 跳过"
}
else {
    # 若原本无 Path 则只写 .bin; 否则追加(保留原有值)
    if ($UserPath) { $NewPath = "$UserPath;$BinDir" } else { $NewPath = $BinDir }
    # 注意: setx 截断到 1024 字符, 超过时改用注册表直写并广播刷新生效
    try {
        [System.Environment]::SetEnvironmentVariable("Path", $NewPath, "User")
        Write-Host "[sun] 已追加用户 PATH: $BinDir"
    }
    catch {
        Write-Host "[sun] 警告: 自动写入用户 PATH 失败, 请手动添加 `"$BinDir`" 到系统环境变量 Path"
    }
}

Write-Host ""
Write-Host "=============================================="
Write-Host " 安装完成!"
Write-Host "  运行   : 新开终端执行  sun  或  sun list"
Write-Host "  全局命令已自动配置, 新终端或注销重登后生效"
Write-Host "  开机自启: 可选, 执行 sun --autostart on"
Write-Host "=============================================="