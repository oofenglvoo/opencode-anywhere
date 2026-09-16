<#
  install-remote.ps1 - B 机(新电脑)环境安装脚本
  用法: 把项目 tools 文件夹(含本脚本)拷到 B 机任意位置,
        管理员 PowerShell 运行:  powershell -NoProfile -ExecutionPolicy Bypass -File .\install-remote.ps1
  功能: 仅安装环境 - Git/Node/Python/opencode-ai/ttkbootstrap(缺才装)
        -> 工具脚本进 PATH -> 创建桌面快捷方式
  会话同步: 不需要 Syncthing, 由 GUI([会话管理]+[会话同步]页)把勾选的会话导出成会话包,
            经 GitHub 私库(HTTPS)上传/并入, 本脚本不涉及.
#>
$ErrorActionPreference = 'Stop'

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

# ---------- 2. 工具脚本 PATH ----------
$projTools = 'D:\PythonProjects\claudeproject\不常用项目\opencode会话和文件同步\tools'
if (Test-Path (Join-Path $projTools 'ocp-gui.cmd')) {
    $toolHome = $projTools
    Say "[2] 项目 tools 已在标准路径, 直接使用: $projTools" 'Green'
} else {
    $toolHome = Join-Path $env:USERPROFILE 'bin'
    New-Item -ItemType Directory -Force -Path $toolHome | Out-Null
    foreach ($file in 'ocp.py', 'ocp.cmd', 'ocp-gui.py', 'ocp-gui.cmd', 'app.ico') {
        $src = Join-Path $PSScriptRoot $file
        if (Test-Path $src) { Copy-Item -LiteralPath $src -Destination $toolHome -Force }
    }
    Say "[2] 已拷贝工具到 $toolHome" 'Green'
}
$k = [Microsoft.Win32.Registry]::CurrentUser.OpenSubKey('Environment', $true)
$userPath = $k.GetValue('Path', '', [Microsoft.Win32.RegistryValueOptions]::DoNotExpandEnvironmentNames)
if (($userPath -split ';') -notcontains $toolHome) {
    $k.SetValue('Path', ($userPath.TrimEnd(';') + ';' + $toolHome), [Microsoft.Win32.RegistryValueKind]::ExpandString)
    Say "[2] 已加入用户 PATH: $toolHome" 'Green'
}

# ---------- 3. 桌面快捷方式 ----------
$ws = New-Object -ComObject WScript.Shell
try {
    $sc = $ws.CreateShortcut("$([Environment]::GetFolderPath('Desktop'))\opencode会话管理.lnk")
    $sc.TargetPath = (Join-Path $toolHome 'ocp-gui.cmd')
    $sc.WorkingDirectory = $toolHome
    $ic = Join-Path $toolHome 'app.ico'
    if (Test-Path $ic) { $sc.IconLocation = $ic }
    $sc.Save()
    Say "[3] 已创建桌面快捷方式: opencode会话管理" 'Green'
} catch { Say "[3] 桌面快捷方式创建失败: $_" 'Yellow' }

Say ''
Say '================= 完成, 接下来在 GUI 里配置同步 =================' -ForegroundColor Yellow
Say '1) 打开本机 GUI:  ocp-gui' -ForegroundColor White
Say '2) 在[会话同步]页填入 GitHub 私库地址(默认 HTTPS, 填 owner/repo 会自动补全)和 PAT, 点[保存并准备仓库]' -ForegroundColor White
Say '3) 同步流程: 一端在[会话管理]勾选会话点[上传所选会话] -> 另一端点[刷新远端会话包]后[下载并并入所选]' -ForegroundColor White
