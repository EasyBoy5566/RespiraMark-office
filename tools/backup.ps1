# backup.ps1 — 備份設定/帳號/裝置權杖/憑證/日誌（IMPROVEMENT_PLAN.md W-205）
# ================================================================================
# 用法（在 respiramark-office 資料夾執行；有外部備份空間時才手動或排程執行）：
#   powershell -ExecutionPolicy Bypass -File tools\backup.ps1 -Dest D:\RespiraMarkBackup
#
# 備份內容：
#   config.json, accounts.json, devices.json,
#   certs\server.pem, certs\server.key（**不含 ca.key**——CA 私鑰依規定離線保存，
#   絕不進自動備份，見 IMPROVEMENT_PLAN.md W-204/F-13），
#   logs\audit.log
#   依目前資源決策，logs\alarm_logs\ 與 logs\sys_logs\ 的七天歷史 DB
#   刻意不納入備份，避免佔用額外空間。
# 打包成 respiramark_backup_<日期時間>.zip，存到 -Dest 指定的資料夾（建議是外接碟
# 或網路磁碟，不要跟伺服器本機是同一顆硬碟）。-KeepDays 內的備份自動保留，較舊的清掉。
#
# 還原步驟：
#   1. 停止伺服器（關掉 python main.py / 停止 Windows 工作排程器的工作）
#   2. 用 Expand-Archive 把備份 zip 解開到暫存資料夾
#   3. 把需要的檔案複製回專案根目錄對應位置（例如只需要救回 accounts.json 就只複製那一個檔）
#   4. 重新啟動伺服器，確認可正常登入/運作

param(
    [Parameter(Mandatory = $true)]
    [string]$Dest,
    [int]$KeepDays = 30
)

$ProjectRoot = Split-Path -Parent $PSScriptRoot
$Timestamp = Get-Date -Format "yyyyMMdd_HHmmss"
$StageDir = Join-Path $env:TEMP "respiramark_backup_stage_$Timestamp"
$ZipPath = Join-Path $Dest "respiramark_backup_$Timestamp.zip"

if (-not (Test-Path $Dest)) {
    New-Item -ItemType Directory -Path $Dest -Force | Out-Null
}
New-Item -ItemType Directory -Path $StageDir -Force | Out-Null

function Copy-IfExists {
    param([string]$RelPath)
    $src = Join-Path $ProjectRoot $RelPath
    if (-not (Test-Path $src)) { return }
    $dst = Join-Path $StageDir $RelPath
    $dstParent = Split-Path -Parent $dst
    if (-not (Test-Path $dstParent)) { New-Item -ItemType Directory -Path $dstParent -Force | Out-Null }
    Copy-Item $src $dst -Recurse -Force
}

Copy-IfExists "config.json"
Copy-IfExists "accounts.json"
Copy-IfExists "devices.json"
Copy-IfExists "certs\server.pem"
Copy-IfExists "certs\server.key"          # 注意：刻意不複製 certs\ca.key
Copy-IfExists "logs\audit.log"

if (Test-Path (Join-Path $ProjectRoot "certs\ca.key")) {
    Write-Warning "偵測到 certs\ca.key 存在於伺服器上——依規定應離線保存於伺服器之外（見 W-204）。本備份腳本不會複製它，但建議儘快處理。"
}

Compress-Archive -Path "$StageDir\*" -DestinationPath $ZipPath -Force
Remove-Item $StageDir -Recurse -Force

Write-Host "已備份 -> $ZipPath"

# 清掉超過保留天數的舊備份
$cutoff = (Get-Date).AddDays(-$KeepDays)
Get-ChildItem $Dest -Filter "respiramark_backup_*.zip" |
    Where-Object { $_.LastWriteTime -lt $cutoff } |
    ForEach-Object {
        Write-Host "刪除逾期備份: $($_.Name)"
        Remove-Item $_.FullName -Force
    }
