# agent-mini 启动器（双击 启动agent-mini.bat 就会跑这个）
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$port = 8770
$python = Join-Path $root ".venv\Scripts\python.exe"

# 1) 第一次运行：建环境 + 装依赖（只做一次）
if (-not (Test-Path $python)) {
    Write-Host "[1/3] 第一次运行：正在创建 Python 环境（约 1 分钟）..." -ForegroundColor Cyan
    python -m venv (Join-Path $root ".venv")
    & $python -m pip install --upgrade pip -q
    & $python -m pip install -r (Join-Path $root "requirements.txt") -q
    Write-Host "      环境就绪" -ForegroundColor Green
}

# 2) 已在运行：只打开页面
$listening = $false
try {
    $c = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction Stop
    if ($c) { $listening = $true }
} catch { }
if ($listening) {
    Write-Host "服务已经在跑了，直接打开页面..." -ForegroundColor Yellow
    Start-Process "http://127.0.0.1:$port/"
    Start-Sleep 2
    exit
}

# 3) 后台等端口就绪后自动开浏览器；前台跑服务（这个窗口就是服务窗口）
$browserJob = Start-Job -ScriptBlock {
    param($p)
    for ($i = 0; $i -lt 60; $i++) {
        Start-Sleep -Milliseconds 500
        try {
            $t = New-Object System.Net.Sockets.TcpClient
            $t.Connect("127.0.0.1", $p)
            $t.Close()
            Start-Process "http://127.0.0.1:$p/"
            return
        } catch { }
    }
} -ArgumentList $port

Write-Host ""
Write-Host "  agent-mini 正在启动..." -ForegroundColor Cyan
Write-Host "  页面地址：http://127.0.0.1:$port/" -ForegroundColor Green
Write-Host "  改 .py 保存即生效；关掉本窗口 = 停止服务" -ForegroundColor DarkGray
Write-Host ""

Set-Location $root
$env:PYTHONIOENCODING = "utf-8"
& $python -m uvicorn app.main:app --host 127.0.0.1 --port $port --reload