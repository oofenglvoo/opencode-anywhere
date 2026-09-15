<#
  oc-sync.ps1 - opencode 多机切换助手 (主动-被动模式)

  用法:
    oc-out    在旧机器上: 检查 opencode 已退出 -> WAL checkpoint 并回主库 -> 写活跃标记 -> 触发/等待 Syncthing 同步
    oc-in     在新机器上: 等待 Syncthing 拉取完成 -> 校验/更新活跃标记
    status    查看 Syncthing 连接与目录同步状态 (若可用)

  无 Syncthing REST 接口时降级为人工确认(在托盘里确认“已是最新”后回车)。
#>
[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [ValidateSet('out', 'in', 'status')]
    [string]$Action = 'status'
)

$ErrorActionPreference = 'Stop'
$DataDir   = Join-Path $env:USERPROFILE '.local\share\opencode'
$DbPath    = Join-Path $DataDir 'opencode.db'
$Marker    = Join-Path $DataDir '.opencode-active-host'
$StTimeout = 120   # seconds to wait for syncthing

function Write-Info($m) { Write-Host "[i] $m" -ForegroundColor Cyan }
function Write-Ok($m)   { Write-Host "[+] $m" -ForegroundColor Green }
function Write-Warn2($m){ Write-Host "[!] $m" -ForegroundColor Yellow }

function Get-SyncthingApi {
    $cfg = Join-Path $env:LOCALAPPDATA 'Syncthing\config.xml'
    if (-not (Test-Path $cfg)) { return $null }
    try {
        [xml]$x = Get-Content -LiteralPath $cfg -Raw -Encoding UTF8
        $g = $x.configuration.gui
        if (-not $g) { return $null }
        $addr = if ($g.address -is [string]) { $g.address } else { $g.address.InnerText }
        if (-not $addr) { $addr = '127.0.0.1:8384' }
        $key = if ($g.apikey -is [string]) { $g.apikey } else { $g.apikey.InnerText }
        if (-not $key) { return $null }
        if ($addr -like '0.0.0.0*') { $addr = "127.0.0.1$(($addr -split ':',2)[1])" }
        elseif ($addr -notlike '127.*' -and $addr -notlike 'localhost*') { $addr = "127.0.0.1$(($addr -split ':',2)[1])" }
        return @{ Base = "http://$addr"; Key = $key }
    } catch { return $null }
}

function Invoke-ST {
    param($Api, [string]$Path, [string]$Method = 'GET')
    Invoke-RestMethod -Uri "$($Api.Base)$Path" -Method $Method -Headers @{ 'X-API-Key' = $Api.Key } -TimeoutSec 10
}

function Test-OpencodeRunning {
    $p = Get-Process -Name 'opencode' -ErrorAction SilentlyContinue
    return [bool]$p
}

function Save-Checkpoint {
    if (-not (Test-Path $DbPath)) { throw "找不到数据库: $DbPath" }
    $py = @'
import sqlite3, sys
con = sqlite3.connect(sys.argv[1], timeout=10)
r = con.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
con.commit(); con.close()
sys.exit(0 if r[0] == 0 else 1)
'@
    $py | python - $DbPath
    if ($LASTEXITCODE -ne 0) { throw 'WAL checkpoint 失败(数据库可能被占用)' }
    Write-Ok "WAL 已并入主库 (checkpoint TRUNCATE)"
}

function Set-Marker {
    "$env:COMPUTERNAME`t$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')" | Set-Content -LiteralPath $Marker -Encoding UTF8
}

function Read-Marker {
    if (Test-Path $Marker) {
        $line = (Get-Content -LiteralPath $Marker -TotalCount 1 -Encoding UTF8).Trim()
        return ($line -split "`t")[0]
    }
    return $null
}

function Wait-Syncthing {
    param($Api, [switch]$Pull)
    $folders = (Invoke-ST $Api '/rest/system/config').folders
    if (-not $folders) { Write-Warn2 'Syncthing 中尚未配置任何目录'; return }
    foreach ($f in $folders) {
        $enc = [uri]::EscapeDataString($f.id)
        Invoke-ST $Api "/rest/db/scan?folder=$enc" -Method Post | Out-Null
    }
    Write-Info "等待同步完成 (最长 $StTimeout 秒)..."
    $deadline = (Get-Date).AddSeconds($StTimeout)
    do {
        Start-Sleep -Seconds 3
        $pending = @()
        foreach ($f in $folders) {
            $enc = [uri]::EscapeDataString($f.id)
            $st = Invoke-ST $Api "/rest/db/status?folder=$enc"
            $need = [int]$st.needBytes
            if ($st.state -ne 'idle' -or $need -gt 0) {
                $mb = [math]::Round($need / 1MB, 1)
                $pending += "$($f.label) [$($st.state), ${mb}MB 待传]"
            }
        }
        if ($pending.Count -eq 0) { Write-Ok '所有目录已同步完成'; return }
        Write-Host ("    进行中: " + ($pending -join '; ')) -ForegroundColor DarkGray
    } while ((Get-Date) -lt $deadline)
    Write-Warn2 '超时: 请在 Syncthing 窗口确认同步完成后再继续'
    Read-Host '确认已同步完成后按回车' | Out-Null
}

switch ($Action) {
    'out' {
        if (Test-OpencodeRunning) { Write-Warn2 '检测到 opencode 仍在运行, 请先全部退出后重试'; exit 1 }
        Save-Checkpoint
        Set-Marker
        $api = Get-SyncthingApi
        if ($api) { try { Wait-Syncthing $api; exit 0 } catch { Write-Warn2 "Syncthing REST 不可用: $_" } }
        Write-Info '请在 Syncthing 托盘中确认三个目录均"已是最新"后, 再在另一台电脑运行 oc-in'
        Read-Host '确认完成后按回车' | Out-Null
    }
    'in' {
        $api = Get-SyncthingApi
        if ($api) { try { Wait-Syncthing $api -Pull } catch { Write-Warn2 "Syncthing REST 不可用: $_"; Read-Host '请在托盘确认已拉取最新后按回车' | Out-Null } }
        else { Read-Host '请在 Syncthing 托盘确认拉取完成后按回车' | Out-Null }
        $prev = Read-Marker
        if ($prev -and $prev -ne $env:COMPUTERNAME) {
            $ans = Read-Host "上次活跃机器是 [$prev], 确认那台已退出 opencode? 输入 yes 继续"
            if ($ans -ne 'yes') { exit 1 }
        }
        Set-Marker
        Write-Ok '本机已标记为活跃机器。用 ocp 挑选任意历史会话即可续接'
    }
    'status' {
        $api = Get-SyncthingApi
        if (-not $api) { Write-Warn2 'Syncthing 未安装或未配置 (未找到 config.xml)'; exit 0 }
        try {
            $names = @{}
            (Invoke-ST $api '/rest/system/config').devices | ForEach-Object { $names[$_.deviceID] = $_.name }
            $cs = (Invoke-ST $api '/rest/system/connections').connections
            $on = $cs.PSObject.Properties | Where-Object { $_.Value.connected } | ForEach-Object {
                if ($names.ContainsKey($_.Name)) { $names[$_.Name] } else { $_.Name.Substring(0, 7) }
            }
            if ($on) { Write-Info ("已连接设备: " + ($on -join ', ')) } else { Write-Warn2 '当前没有其他已连接的 Syncthing 设备' }
            $folders = (Invoke-ST $api '/rest/system/config').folders
            foreach ($f in $folders) {
                $enc = [uri]::EscapeDataString($f.id)
                $st = Invoke-ST $api "/rest/db/status?folder=$enc"
                Write-Info ("{0}: {1} (待传 {2:N1}MB)" -f $f.label, $st.state, ($st.needBytes / 1MB))
            }
        } catch { Write-Warn2 "Syncthing 正在运行但 REST 调用失败: $_" }
    }
}
