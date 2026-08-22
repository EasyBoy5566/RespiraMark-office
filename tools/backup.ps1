# backup.ps1 — 週備份（IMPROVEMENT_PLAN.md W-205；備份原則見下）
# ================================================================================
# 備份原則（2026-08-22 定案，對應資訊室表單二「備份」欄的「週備份留 3 週」）：
#   **每週備份一次，保留最近 3 份，四週循環。**
#
# 備份內容：
#   config.json、accounts.json、devices.json
#   certs\server.pem、certs\server.key
#     （**不含 ca.key**——CA 私鑰依規定離線保存，絕不進自動備份，見 W-204/F-13）
#   logs\audit.log 與**全部已輪替的** audit.log.YYYY-MM-DD
#     （稽核日誌本機保留 190 天，備份必須連輪替檔一起帶走，否則還原後只剩當天那份，
#      交代不了防護基準第 16 點要求的 6 個月保存）
#   logs\audit_logs\、logs\alarm_logs\、logs\sys_logs\ 底下的 SQLite
#     （警報 episode 與 Pi 系統健康，本機各保留 10 天）
#
# 🚨 SQLite 一律走 tools\backup_db.py（SQLite backup API），**絕不直接複製檔案**：
#    DB 跑 WAL 模式，最近的異動還在 -wal 裡，運行中複製會拿到撕裂的檔案。
#    純文字檔（設定、憑證、日誌）才用 Copy-Item。
#
# 用法（在 respiramark-office 資料夾執行）：
#   powershell -ExecutionPolicy Bypass -File tools\backup.ps1 -Dest D:\VentMonitorBackup -WhatIf   # 先預覽
#   powershell -ExecutionPolicy Bypass -File tools\backup.ps1 -Dest D:\VentMonitorBackup           # 實際備份一次
#   powershell -ExecutionPolicy Bypass -File tools\backup.ps1 -Dest D:\VentMonitorBackup -Register # 註冊每週自動執行
#
# -Dest 建議指向外接碟或網路磁碟，**不要跟伺服器是同一顆硬碟**——硬碟壞掉時
# 備份跟正本一起陪葬就失去意義了。
#
# 保留策略是「保留最近 N 份」而不是「刪掉幾天前的」：某週因為機器關機而漏跑時，
# 依日期刪除有機會把僅存的備份全部清掉，依份數保留則永遠留得住最近 3 份。
#
# 移除自動排程：
#   Unregister-ScheduledTask -TaskName "VentMonitorBackup" -Confirm:$false
#
# 還原步驟：
#   1. 停止伺服器（停止工作排程器的 RespiraMarkOffice 工作 / 關掉 python main.py）
#   2. Expand-Archive 把備份 zip 解開到暫存資料夾
#   3. 複製需要的檔案回專案根目錄對應位置（只需救回 accounts.json 就只複製那一個）
#      —— SQLite 直接複製回去即可，備份出來的是完整單一檔案，沒有 -wal 要處理
#   4. 重新啟動伺服器，確認可正常登入、看板正常

[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [Parameter(Mandatory = $true)]
    [string]$Dest,
    [int]$Keep = 3,                       # 保留最近幾份（週備份 × 3 = 涵蓋約三週）
    [string]$PythonExe = "python",
    [switch]$Register,                    # 註冊每週自動執行的工作排程
    [string]$TaskName = "VentMonitorBackup",
    [PSCredential]$Credential
)

$ProjectRoot = Split-Path -Parent $PSScriptRoot
$BackupDbPy = Join-Path $PSScriptRoot "backup_db.py"

# ── -Register：註冊每週自動備份，之後就不用有人記得手動跑 ──────────────
if ($Register) {
    $thisScript = Join-Path $PSScriptRoot "backup.ps1"
    $arguments = "-ExecutionPolicy Bypass -File `"$thisScript`" -Dest `"$Dest`" -Keep $Keep"
    $action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument $arguments -WorkingDirectory $ProjectRoot
    # 每週日 03:00：離峰時段，且 -StartWhenAvailable 讓當時關機的話開機後補跑
    $trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Sunday -At 3am
    $settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Hours 2)
    if ($Credential) {
        if ($PSCmdlet.ShouldProcess($TaskName, "Register-ScheduledTask 每週備份（帳號: $($Credential.UserName)）")) {
            Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
                -Settings $settings -User $Credential.UserName `
                -Password $Credential.GetNetworkCredential().Password -Force | Out-Null
        }
    }
    else {
        if ($PSCmdlet.ShouldProcess($TaskName, "Register-ScheduledTask 每週備份（帳號: SYSTEM）")) {
            $principal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest
            Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
                -Settings $settings -Principal $principal -Force | Out-Null
        }
    }
    Write-Host "已註冊每週備份工作: $TaskName（每週日 03:00 -> $Dest，保留 $Keep 份）"
    Write-Host "驗收：Start-ScheduledTask -TaskName $TaskName 手動觸發一次，確認 $Dest 出現 zip"
    return
}

# ── 實際備份 ────────────────────────────────────────────────────────────
$Timestamp = Get-Date -Format "yyyyMMdd_HHmmss"
$StageDir = Join-Path $env:TEMP "ventmonitor_backup_stage_$Timestamp"
$ZipPath = Join-Path $Dest "ventmonitor_backup_$Timestamp.zip"

if ($PSCmdlet.ShouldProcess($ZipPath, "建立備份")) {
    if (-not (Test-Path $Dest)) { New-Item -ItemType Directory -Path $Dest -Force | Out-Null }
    New-Item -ItemType Directory -Path $StageDir -Force | Out-Null
}

$failed = @()

function Copy-Plain {
    <#  純文字/二進位檔（設定、憑證、文字日誌）直接複製。
        RelPath 可含萬用字元，用來一次帶走全部輪替後的 audit.log.*  #>
    param([string]$RelPath)
    $matches = @(Get-ChildItem (Join-Path $ProjectRoot $RelPath) -ErrorAction SilentlyContinue)
    if ($matches.Count -eq 0) { return }
    foreach ($item in $matches) {
        $relative = $item.FullName.Substring($ProjectRoot.Length).TrimStart('\')
        Write-Host "  + $relative"
        if (-not $PSCmdlet.ShouldProcess($relative, "複製")) { continue }
        $dst = Join-Path $StageDir $relative
        $dstParent = Split-Path -Parent $dst
        if (-not (Test-Path $dstParent)) { New-Item -ItemType Directory -Path $dstParent -Force | Out-Null }
        Copy-Item $item.FullName $dst -Force
    }
}

function Copy-Sqlite {
    <#  SQLite 一律走 backup_db.py，絕不直接複製（見檔頭紅線）。 #>
    param([string]$RelDir)
    $dir = Join-Path $ProjectRoot $RelDir
    if (-not (Test-Path $dir)) { return }
    foreach ($db in @(Get-ChildItem $dir -Filter "*.sqlite3" -ErrorAction SilentlyContinue)) {
        $relative = $db.FullName.Substring($ProjectRoot.Length).TrimStart('\')
        Write-Host "  + $relative (SQLite backup API)"
        if (-not $PSCmdlet.ShouldProcess($relative, "SQLite 安全備份")) { continue }
        $dst = Join-Path $StageDir $relative
        & $PythonExe $BackupDbPy $db.FullName $dst --quiet
        if ($LASTEXITCODE -ne 0) {
            $script:failed += $relative
            Write-Warning "備份失敗: $relative"
        }
    }
}

Write-Host "備份內容："
Copy-Plain "config.json"
Copy-Plain "accounts.json"
Copy-Plain "devices.json"
Copy-Plain "certs\server.pem"
Copy-Plain "certs\server.key"            # 注意：刻意不複製 certs\ca.key
Copy-Plain "logs\audit.log*"             # 含全部輪替檔（190 天稽核紀錄）
Copy-Sqlite "logs\audit_logs"
Copy-Sqlite "logs\alarm_logs"
Copy-Sqlite "logs\sys_logs"

if (Test-Path (Join-Path $ProjectRoot "certs\ca.key")) {
    Write-Warning "偵測到 certs\ca.key 存在於伺服器上——依規定應離線保存於伺服器之外（見 W-204）。本腳本不會複製它，但建議儘快處理。"
}

if ($PSCmdlet.ShouldProcess($ZipPath, "打包並清理")) {
    Compress-Archive -Path "$StageDir\*" -DestinationPath $ZipPath -Force
    Remove-Item $StageDir -Recurse -Force
    $sizeMb = [math]::Round((Get-Item $ZipPath).Length / 1MB, 1)
    Write-Host "已備份 -> $ZipPath（$sizeMb MB）"

    # 只保留最近 $Keep 份（依檔名時間戳排序，不依修改時間——複製到別處後仍正確）
    $old = @(Get-ChildItem $Dest -Filter "ventmonitor_backup_*.zip" |
             Sort-Object Name -Descending | Select-Object -Skip $Keep)
    foreach ($item in $old) {
        Write-Host "刪除逾期備份: $($item.Name)"
        Remove-Item $item.FullName -Force
    }
}

if ($failed.Count -gt 0) {
    throw "有 $($failed.Count) 個資料庫備份失敗: $($failed -join ', ')"
}
