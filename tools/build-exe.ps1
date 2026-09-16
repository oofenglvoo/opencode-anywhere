<#
  build-exe.ps1 - 把 ocp-gui.py 打包成带图标的独立 exe
  用法: powershell -NoProfile -ExecutionPolicy Bypass -File .\build-exe.ps1
  产物: ..\dist\opencode-anywhere.exe
  说明: 需要 pip 安装 pyinstaller (脚本会自动检测并尝试安装)
#>
$ErrorActionPreference = 'Stop'
# A shell spawned from the GUI inherits PyInstaller's _PYI_*/_MEIPASS2/TCL_LIBRARY,
# which point at a deleted _MEIxxxx dir. That makes PyInstaller's tkinter hook fail,
# and the resulting exe silently loses tkinter ("No module named 'tkinter'").
# Clear them so the build is reproducible from any parent process. (keep ASCII)
Get-ChildItem env: | Where-Object { $_.Name -like '_PYI_*' -or
    $_.Name -in @('_MEIPASS2', 'TCL_LIBRARY', 'TK_LIBRARY') } |
    ForEach-Object { Remove-Item ('Env:' + $_.Name) -ErrorAction SilentlyContinue }
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$root = Split-Path -Parent $here
$entry = Join-Path $here 'ocp-gui.py'
$icon = Join-Path $here 'app.ico'
$spec = Join-Path $root 'opencode-anywhere.spec'

try { python -c "import PyInstaller" 2>$null; if ($LASTEXITCODE -ne 0) { throw 'no-pyinstaller' } }
catch { Write-Host '[i] 安装 pyinstaller...'; python -m pip install pyinstaller }

# 打包: onefile 单文件, 无控制台, 内嵌 app.ico 作为 exe/任务栏图标, 并随包附带 app.ico 供运行时窗口使用
# 注意: ttkbootstrap 硬依赖 Pillow(style/theme.py 等 from PIL import ...),
#       不能排除 PIL, 否则冻结后 import ttkbootstrap 失败 -> HAS_TB=False -> 深色主题失效
# 排除运行时确实不需要的重依赖(numpy/pandas/scipy/matplotlib/pygame), 避免体积膨胀
python -m PyInstaller --noconfirm --clean --onefile --windowed `
    --name opencode-anywhere `
    --icon $icon `
    --add-data "$icon;." `
    --exclude-module numpy `
    --exclude-module pandas `
    --exclude-module scipy `
    --exclude-module matplotlib `
    --exclude-module pygame `
    --distpath (Join-Path $root 'dist') `
    --workpath (Join-Path $root 'build') `
    --specpath $root `
    $entry

$exe = Join-Path $root 'dist\opencode-anywhere.exe'
$warn = Join-Path $root 'build\opencode-anywhere\warn-opencode-anywhere.txt'
if ((Test-Path $warn) -and (Select-String -Path $warn -Pattern '^missing module named tkinter ' -Quiet)) {
    throw 'tkinter was NOT bundled (see warn-opencode-anywhere.txt); clear _PYI_*/TCL_LIBRARY and rebuild'
}
if (Test-Path $exe) {
    Write-Host "[+] 打包完成: $exe" -ForegroundColor Green
    Write-Host ("    大小: {0:N1} MB" -f ((Get-Item $exe).Length / 1MB)) -ForegroundColor Green
} else {
    throw '打包失败: 未找到 dist\opencode-anywhere.exe'
}
