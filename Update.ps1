$ToolArguments = @($args)
$ErrorActionPreference = 'Stop'
$env:PYTHONUTF8 = '1'
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
try {
    $pioRoot = if ($env:PLATFORMIO_CORE_DIR) { $env:PLATFORMIO_CORE_DIR } else { Join-Path $env:USERPROFILE '.platformio' }
    $pythonPath = if ($env:BYHUAV_PIO_PYTHON) { $env:BYHUAV_PIO_PYTHON } else { Join-Path $pioRoot 'penv\Scripts\python.exe' }
    if (-not (Test-Path -LiteralPath $pythonPath)) { throw '请先在 VS Code 安装 PlatformIO IDE，并等待初始化完成。' }
    & $pythonPath -u (Join-Path $PSScriptRoot 'update.py') @ToolArguments
    exit $LASTEXITCODE
} catch {
    Write-Host ('[停止] ' + $_.Exception.Message) -ForegroundColor Red
    exit 1
}
