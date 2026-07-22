# lock_permissions.ps1 — 鎖定敏感檔案存取權限（IMPROVEMENT_PLAN.md W-204）
# ================================================================================
# 把帳號檔、裝置權杖檔、TLS 憑證私鑰、各種 log 只開放給「執行伺服器的服務帳號」
# 與系統管理員讀寫，其他一般使用者帳號讀不到（對應 F-13）。
#
# ⚠️ 請在正式伺服器上執行，不要在開發用電腦上執行。先用 -DryRun 預覽會下的
#    icacls 指令，確認無誤再正式執行。
#
# 用法（在正式伺服器、以系統管理員身分開 PowerShell）：
#   cd C:\path\to\respiramark-office
#   powershell -ExecutionPolicy Bypass -File tools\lock_permissions.ps1 -ServiceAccount respiramark-svc -DryRun
#   powershell -ExecutionPolicy Bypass -File tools\lock_permissions.ps1 -ServiceAccount respiramark-svc
#
# ServiceAccount 請填 tools\setup_service.ps1 設定服務時用的那個帳號
# （例如專用低權限本機帳號 respiramark-svc；未另外設定服務帳號則填實際執行
# python main.py 的那個 Windows 帳號）。

[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$ServiceAccount,
    [switch]$DryRun
)

$ProjectRoot = Split-Path -Parent $PSScriptRoot
# 敏感檔案/目錄：帳號密碼雜湊、裝置 token 雜湊、一般設定、TLS 私鑰、各種 log
$targets = @("accounts.json", "devices.json", "config.json", "certs", "logs")

foreach ($rel in $targets) {
    $path = Join-Path $ProjectRoot $rel
    if (-not (Test-Path $path)) {
        Write-Host "略過（不存在）: $rel"
        continue
    }
    # /inheritance:r 先切斷繼承（不然一般使用者可能透過上層資料夾權限讀到）；
    # 只保留服務帳號與 Administrators 完整權限，(OI)(CI) 讓子資料夾/檔案一併套用
    $args = @($path, "/inheritance:r", "/grant:r",
              "${ServiceAccount}:(OI)(CI)F", "Administrators:(OI)(CI)F")
    if ($DryRun) {
        Write-Host "[DryRun] icacls $($args -join ' ')"
    } else {
        Write-Host "鎖定: $rel"
        & icacls @args | Out-Null
    }
}

Write-Host ""
Write-Host "-----------------------------------------------------------"
Write-Host "ca.key 提醒（W-204）：certs\ca.key（CA 發證私鑰）不應該長駐伺服器。"
Write-Host "  簽完伺服器憑證後，請把 ca.key 複製到兩份離線 USB，然後從伺服器上"
Write-Host "  刪除；下次要重簽憑證（例如換 IP）時再暫時取回，簽完立刻再次移除。"
if (Test-Path (Join-Path $ProjectRoot "certs\ca.key")) {
    Write-Warning "偵測到 certs\ca.key 目前仍在伺服器上，請依上述步驟處理。"
}
