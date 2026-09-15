<#
  install-remote.ps1 - B 机(新电脑)一键安装脚本
  用法: 把项目 tools 文件夹(含本脚本及 ocp/oc 系列文件)拷到 B 机任意位置,
        管理员 PowerShell 运行:  powershell -NoProfile -ExecutionPolicy Bypass -File .\install-remote.ps1
  功能: 安装 Node/Git/Syncthing(缺才装) -> 初始化并启动 Syncthing -> 预添加 A 机设备与 3 个同步目录
        -> 工具脚本进 PATH(若项目已同步则直接用项目内 tools) -> 输出 B 机 DeviceID 供在 A 机跑 oc-pair
#>
$ErrorActionPreference = 'Stop'
$A_DEVICE_ID = '45OYR3E-4TU7ZIP-NF6U4M4-DTJKNVO-RHTDPKC-7FSFEYV-QYNSCFX-QO2OKAE'
$A_NAME      = 'DESKTOP-A (opencode主)'

function Say($m, $c = 'Cyan')   { Write-Host $m -ForegroundColor $c }
function Have($cmd) { [bool](Get-Command $cmd -ErrorAction SilentlyContinue) }
$IsAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
           ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)

function Winget-Install($id) {
    if (-not $IsAdmin) { throw "缺少组件需管理员权限安装 ($id), 请用管理员 PowerShell 重跑" }
    winget install --id $id -e --accept-package-agreements --accept-source-agreements --silent
    $env:Path = [Environment]::GetEnvironmentVariable('Path', 'Machine') + ';' +
                [Environment]::GetEnvironmentVariable('Path', 'User')
}

# ---------- 1. 依赖 ----------
if (-not (Have git))   { Say '[1] 安装 Git...';   Winget-Install 'Git.Git' }
if (-not (Have node))  { Say '[1] 安装 Node.js...'; Winget-Install 'OpenJS.NodeJS.LTS' }
if (-not (Have python)) { Say '[1] 安装 Python...'; Winget-Install 'Python.Python.3.12' }
if (-not (Have opencode)) { Say '[1] npm 安装 opencode-ai...'; npm install -g opencode-ai }
try { python -c "import ttkbootstrap" 2>$null; if ($LASTEXITCODE -ne 0) { Say '[1] pip 安装 ttkbootstrap...'; python -m pip install --user ttkbootstrap } } catch { Say '[1] pip 安装 ttkbootstrap...'; python -m pip install --user ttkbootstrap }

# ---------- 2. Syncthing ----------
$stExe = $null
if (Have syncthing) { $stExe = (Get-Command syncthing).Source }
else {
    Say '[2] 安装 Syncthing...'
    winget install --id Syncthing.Syncthing -e --accept-package-agreements --accept-source-agreements --silent
    $env:Path = [Environment]::GetEnvironmentVariable('Path', 'Machine') + ';' +
                [Environment]::GetEnvironmentVariable('Path', 'User')
}
if (-not $stExe) {
    $stExe = Get-ChildItem "$env:LOCALAPPDATA\Microsoft\WinGet\Packages" -Recurse -Filter 'syncthing.exe' -ErrorAction SilentlyContinue |
             Select-Object -First 1 -ExpandProperty FullName
}
if (-not $stExe) { throw '找不到 syncthing.exe, 请手动安装后重跑' }

$cfg = Join-Path $env:LOCALAPPDATA 'Syncthing\config.xml'
if (-not (Test-Path $cfg)) { & $stExe generate | Out-Null }
$linksDir = "$env:LOCALAPPDATA\Microsoft\WinGet\Links"
New-Item -ItemType Directory -Force -Path $linksDir | Out-Null
if (-not (Test-Path "$linksDir\syncthing.exe")) { New-Item -ItemType HardLink -Path "$linksDir\syncthing.exe" -Target $stExe | Out-Null }

if (-not (Get-Process syncthing -ErrorAction SilentlyContinue)) {
    Start-Process -FilePath $stExe -ArgumentList 'serve', '--no-browser' -WindowStyle Minimized
}
Say '[2] 等待 Syncthing 启动...'
$api = $null
for ($i = 0; $i -lt 30; $i++) {
    Start-Sleep -Seconds 1
    try {
        [xml]$x = Get-Content -LiteralPath $cfg -Raw -Encoding UTF8
        $g = $x.configuration.gui
        $key = if ($g.apikey -is [string]) { $g.apikey } else { $g.apikey.InnerText }
        $api = Invoke-RestMethod -Uri 'http://127.0.0.1:8384/rest/system/status' -Headers @{ 'X-API-Key' = $key } -TimeoutSec 5
        break
    } catch { }
}
if (-not $api) { throw 'Syncthing REST 启动超时' }
Say "[2] Syncthing 已运行, 本机 DeviceID: $($api.myID)" 'Green'

# ---------- 3. 开机自启 ----------
$ws = New-Object -ComObject WScript.Shell
$lnk = $ws.CreateShortcut("$env:APPDATA\Microsoft\Windows\Start Menu\Programs\Startup\Syncthing.lnk")
$lnk.TargetPath = $stExe; $lnk.Arguments = 'serve --no-browser'; $lnk.WindowStyle = 7; $lnk.Save()

# ---------- 4. 预添加 A 机设备 + 创建 3 个同步目录 ----------
$H = @{ 'X-API-Key' = $key }
function Send-Json($Uri, $Method, $Obj) {
    $json = $Obj | ConvertTo-Json -Depth 8
    Invoke-RestMethod -Uri $Uri -Method $Method -Headers $H -ContentType 'application/json' -Body ([System.Text.Encoding]::UTF8.GetBytes($json))
}
$devicesRaw = Invoke-RestMethod -Uri 'http://127.0.0.1:8384/rest/config/devices' -Headers $H
if (@($devicesRaw).deviceID -notcontains $A_DEVICE_ID) {
    Send-Json 'http://127.0.0.1:8384/rest/config/devices' 'Post' @{ deviceID = $A_DEVICE_ID; name = $A_NAME }
    Say '[4] 已添加 A 机设备' 'Green'
}
$dataDir  = Join-Path $env:USERPROFILE '.local\share\opencode'
$confDir  = Join-Path $env:USERPROFILE '.config\opencode'
$projDir  = 'D:\PythonProjects\claudeproject'
if (-not (Test-Path 'D:\')) { throw '本机没有 D 盘, 请创建/挂载 D 盘后重跑(会话以绝对路径归属项目, 需两机路径一致)' }
$ver = @{ type = 'staggered'; params = @{ maxAge = '2592000'; cleanInterval = '3600' } }
$dev = @(@{ deviceID = $api.myID }, @{ deviceID = $A_DEVICE_ID })
$folderSpecs = @(
    @{ id = 'opencode-data';   label = 'opencode session data';  path = $dataDir },
    @{ id = 'opencode-config'; label = 'opencode config';        path = $confDir },
    @{ id = 'claudeproject';   label = 'claudeproject projects'; path = $projDir }
)
$existingRaw = Invoke-RestMethod -Uri 'http://127.0.0.1:8384/rest/config/folders' -Headers $H
$existing = @($existingRaw)
foreach ($spec in $folderSpecs) {
    New-Item -ItemType Directory -Force -Path $spec.path | Out-Null
    if ($existing.id -contains $spec.id) { Say "[4] 目录已存在: $($spec.label)" 'DarkGray'; continue }
    $f = $spec + @{ devices = $dev; rescanIntervalS = 300; fsWatcherEnabled = $true; versioning = $ver }
    Send-Json 'http://127.0.0.1:8384/rest/config/folders' 'Post' $f
    Say "[4] 已创建同步目录: $($spec.label) -> $($spec.path)" 'Green'
}
# .stignore 无需创建: A 机会自动把忽略规则同步过来

# ---------- 5. 工具脚本 PATH ----------
$projTools = 'D:\PythonProjects\claudeproject\不常用项目\opencode会话和文件同步\tools'
if (Test-Path (Join-Path $projTools 'ocp-gui.cmd')) {
    $toolHome = $projTools
    Say "[5] 项目 tools 已随 Syncthing 同步到位, 直接使用: $projTools" 'Green'
} else {
    $toolHome = Join-Path $env:USERPROFILE 'bin'
    New-Item -ItemType Directory -Force -Path $toolHome | Out-Null
    foreach ($file in 'ocp.py', 'ocp.cmd', 'ocp-gui.py', 'ocp-gui.cmd', 'app.ico', 'oc-sync.ps1', 'oc-out.cmd', 'oc-in.cmd', 'oc-status.cmd') {
        $src = Join-Path $PSScriptRoot $file
        if (Test-Path $src) { Copy-Item -LiteralPath $src -Destination $toolHome -Force }
    }
    Say "[5] 项目尚未同步到位, 已拷贝工具到 $toolHome; 目录同步完成后重跑本脚本可自动改用项目内版本" 'Green'
}
$k = [Microsoft.Win32.Registry]::CurrentUser.OpenSubKey('Environment', $true)
$userPath = $k.GetValue('Path', '', [Microsoft.Win32.RegistryValueOptions]::DoNotExpandEnvironmentNames)
$add = @($toolHome, $linksDir) | Where-Object { ($userPath -split ';') -notcontains $_ }
if ($add) {
    $k.SetValue('Path', ($userPath.TrimEnd(';') + ';' + ($add -join ';')), [Microsoft.Win32.RegistryValueKind]::ExpandString)
    Say "[5] 已加入用户 PATH: $($add -join '; ')" 'Green'
}
# 桌面快捷方式
try {
    $sc = $ws.CreateShortcut("$([Environment]::GetFolderPath('Desktop'))\opencode会话管理.lnk")
    $sc.TargetPath = (Join-Path $toolHome 'ocp-gui.cmd')
    $sc.WorkingDirectory = $toolHome
    $ic = Join-Path $toolHome 'app.ico'
    if (Test-Path $ic) { $sc.IconLocation = $ic }
    $sc.Save()
    Say "[5] 已创建桌面快捷方式: opencode会话管理" 'Green'
} catch { Say "[5] 桌面快捷方式创建失败: $_" 'Yellow' }

Say ''
Say '================= 完成, 接下来只需两步 =================' -ForegroundColor Yellow
Say "1) 记下本机 DeviceID: $($api.myID)" -ForegroundColor White
Say "   在 A 机任意终端运行:  oc-pair $($api.myID)" -ForegroundColor White
Say '2) 两侧连上后 A 机退出 opencode 并运行 oc-out, B 机运行 oc-in, 然后 ocp 选会话续接' -ForegroundColor White
Say '注意: 本脚本需与 tools 目录下其他文件放在同一文件夹内一起拷到 B 机' -ForegroundColor DarkGray
