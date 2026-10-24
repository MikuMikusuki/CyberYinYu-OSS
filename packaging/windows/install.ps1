# CyberYinYu Windows installer.
# Usage: powershell -ExecutionPolicy Bypass -File .\install.ps1
$ErrorActionPreference = "Stop"
$AppDir = $PSScriptRoot
Set-Location $AppDir

$PythonExe = "python"
$PythonArgs = @()
if (Get-Command py -ErrorAction SilentlyContinue) {
    $PythonExe = "py"
    $PythonArgs = @("-3")
} elseif (-not (Get-Command python -ErrorAction SilentlyContinue)) {
    throw "找不到 Python。请安装 Python 3.10+ 并加入 PATH。"
}

& $PythonExe @PythonArgs -c "import sys; raise SystemExit(0 if sys.version_info >= (3,10) else 1)"
if ($LASTEXITCODE -ne 0) {
    throw "需要 Python 3.10 或更高版本。"
}

Write-Host "[1/3] 创建虚拟环境" -ForegroundColor Cyan
if (-not (Test-Path ".venv\Scripts\python.exe")) {
    & $PythonExe @PythonArgs -m venv .venv
}

Write-Host "[2/3] 安装依赖" -ForegroundColor Cyan
& ".venv\Scripts\python.exe" -m pip install --upgrade pip
& ".venv\Scripts\python.exe" -m pip install -r requirements.txt

Write-Host "[3/3] 初始化配置" -ForegroundColor Cyan
New-Item -ItemType Directory -Force -Path "logs", "data" | Out-Null
if (-not (Test-Path "otowa.env")) {
    Copy-Item ".env.example" "otowa.env"
    Write-Host "已生成 otowa.env，请填写 BOT_QQ / NAPCAT_TOKEN / AI_API_KEY / OWNER_QQ。" -ForegroundColor Yellow
} else {
    Write-Host "保留现有 otowa.env" -ForegroundColor Yellow
}

Write-Host ""
Write-Host "安装完成。下一步：" -ForegroundColor Green
Write-Host "  1. 编辑 otowa.env"
Write-Host "  2. 在 NapCat 中配置 HTTP 3000 与反向 WS ws://127.0.0.1:8080/onebot/v11/ws"
Write-Host "  3. 双击 start_bot.bat"
