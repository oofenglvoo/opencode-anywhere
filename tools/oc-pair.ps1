<#
  oc-pair.ps1 - 在 A 机(本机)上执行, 把 B 机设备加入 Syncthing 并共享全部 3 个目录.
  用法: oc-pair <B机DeviceID>
#>
param(
    [Parameter(Mandatory = $true, Position = 0)]
    [string]$RemoteID,
    [string]$RemoteName = 'Remote-PC'
)
$ErrorActionPreference = 'Stop'
$cfgFile = Join-Path $env:LOCALAPPDATA 'Syncthing\config.xml'
if (-not (Test-Path $cfgFile)) { throw '未找到 Syncthing config.xml' }
[xml]$x = Get-Content -LiteralPath $cfgFile -Raw -Encoding UTF8
$g = $x.configuration.gui
$addr = if ($g.address -is [string]) { $g.address } else { $g.address.InnerText }
$key = if ($g.apikey -is [string]) { $g.apikey } else { $g.apikey.InnerText }
$H = @{ 'X-API-Key' = $key }
$Base = "http://$addr"
$myId = (Invoke-RestMethod -Uri "$Base/rest/system/status" -Headers $H).myID

function Send-Json($Uri, $Method, $Obj) {
    $json = $Obj | ConvertTo-Json -Depth 8
    Invoke-RestMethod -Uri $Uri -Method $Method -Headers $H -ContentType 'application/json' -Body ([Text.Encoding]::UTF8.GetBytes($json))
}

# 1. 添加远端设备
$devicesRaw = Invoke-RestMethod -Uri "$Base/rest/config/devices" -Headers $H
$devices = @($devicesRaw)
if ($devices.deviceID -contains $RemoteID) {
    Write-Host "[i] 设备已存在, 跳过添加" -ForegroundColor Cyan
} else {
    Send-Json "$Base/rest/config/devices" 'Post' @{ deviceID = $RemoteID; name = $RemoteName }
    Write-Host "[+] 已添加设备 $RemoteName ($($RemoteID.Substring(0,7))...)" -ForegroundColor Green
}

# 2. 每个 folder 增加远端共享
$foldersRaw = Invoke-RestMethod -Uri "$Base/rest/config/folders" -Headers $H
foreach ($f in @($foldersRaw)) {
    if ($f.devices.deviceID -notcontains $RemoteID) {
        $f.devices += @{ deviceID = $RemoteID; encryptionPassword = ''; introducedBy = '' }
        $enc = [uri]::EscapeDataString($f.id)
        Send-Json "$Base/rest/config/folders/$enc" 'Put' $f
        Write-Host "[+] 目录 [$($f.label)] 已共享给远端" -ForegroundColor Green
    } else {
        Write-Host "[i] 目录 [$($f.label)] 已包含远端" -ForegroundColor Cyan
    }
}
Write-Host "`n[完成] 等 B 机上线后两侧会自动连接并同步. 在 B 机运行 oc-in 后即可用 ocp 续接会话" -ForegroundColor Green
