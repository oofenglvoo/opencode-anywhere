<#
  build-exe.ps1 - 把 ocp-gui.py 打包成带图标的独立 exe
  用法: powershell -NoProfile -ExecutionPolicy Bypass -File .\build-exe.ps1
  产物: ..\dist\opencode-anywhere.exe
  说明: 需要 pip 安装 pyinstaller (脚本会自动检测并尝试安装)
#>
$ErrorActionPreference = 'Stop'
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$root = Split-Path -Parent $here
$entry = Join-Path $here 'ocp-gui.py'
$icon = Join-Path $here 'app.ico'
$spec = Join-Path $root 'opencode-anywhere.spec'

try { python -c "import PyInstaller" 2>$null; if ($LASTEXITCODE -ne 0) { throw 'no-pyinstaller' } }
catch { Write-Host '[i] 安装 pyinstaller...'; python -m pip install pyinstaller }

# 打包: onefile 单文件, 无控制台, 内嵌 app.ico 作为 exe/任务栏图标, 并随包附带 app.ico 供运行时窗口使用
# 排除运行时不需要的重依赖(numpy/PIL/pandas 等), 避免被间接钩子拖入导致体积膨胀
python -m PyInstaller --noconfirm --clean --onefile --windowed `
    --name opencode-anywhere `
    --icon $icon `
    --add-data "$icon;." `
    --exclude-module numpy `
    --exclude-module PIL `
    --exclude-module pandas `
    --exclude-module scipy `
    --exclude-module matplotlib `
    --distpath (Join-Path $root 'dist') `
    --workpath (Join-Path $root 'build') `
    --specpath $root `
    $entry

$exe = Join-Path $root 'dist\opencode-anywhere.exe'
if (Test-Path $exe) {
    Write-Host "[+] 打包完成: $exe" -ForegroundColor Green
    Write-Host ("    大小: {0:N1} MB" -f ((Get-Item $exe).Length / 1MB)) -ForegroundColor Green
} else {
    throw '打包失败: 未找到 dist\opencode-anywhere.exe'
}
