# setup_service.ps1 — 服務化與自動復原（Windows 工作排程器，IMPROVEMENT_PLAN.md W-202）
# ================================================================================
# 這支腳本會在「執行它的這台機器」上註冊一個開機自動啟動＋失敗自動重啟的
# 工作排程器工作，讓伺服器可以無人值守運行。
#
# ⚠️ 請在正式伺服器上執行，不要在開發用電腦上執行。先用 -WhatIf 預覽，
#    確認要做的事沒問題再正式執行。
#
# 用法（在正式伺服器、以系統管理員身分開 PowerShell）：
#   cd C:\path\to\respiramark-office
#   powershell -ExecutionPolicy Bypass -File tools\setup_service.ps1 -WhatIf        # 先預覽
#   powershell -ExecutionPolicy Bypass -File tools\setup_service.ps1                # 正式建立
#
# 建議先在伺服器建立一個專用的低權限本機帳號（例如 respiramark-svc，不是系統
# 管理員），只給它這個專案資料夾的讀寫權限，執行時用 -Credential 指定該帳號：
#   $cred = Get-Credential   # 互動輸入 respiramark-svc 的密碼
#   powershell -ExecutionPolicy Bypass -File tools\setup_service.ps1 -Credential $cred
# 不指定 -Credential 時預設用 SYSTEM 帳號執行（權限較高，僅限暫時測試用，
# 正式環境務必改用專用帳號）。
#
# 移除這個工作排程器工作：
#   Unregister-ScheduledTask -TaskName "RespiraMarkOffice" -Confirm:$false
#
# 驗收（T-202）：重開機後儀表板應可正常連上；工作管理員強殺 python.exe 後，
# 約 1 分鐘內應自動復活且 Pi 自動重連（Pi 端本來就有自動重連機制）。
# start_server.bat 仍保留，手動除錯時直接雙擊執行即可，不受這支腳本影響。

[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [string]$TaskName = "RespiraMarkOffice",
    [string]$PythonExe = "python",
    [PSCredential]$Credential
)

$ProjectRoot = Split-Path -Parent $PSScriptRoot
$MainPy = Join-Path $ProjectRoot "main.py"

if (-not (Test-Path $MainPy)) {
    throw "找不到 $MainPy，請確認這支腳本放在專案的 tools\ 資料夾底下執行。"
}

$action = New-ScheduledTaskAction -Execute $PythonExe -Argument "`"$MainPy`"" -WorkingDirectory $ProjectRoot
$trigger = New-ScheduledTaskTrigger -AtStartup
$settings = New-ScheduledTaskSettingsSet `
    -RestartCount 999 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -StartWhenAvailable

if ($Credential) {
    if ($PSCmdlet.ShouldProcess($TaskName, "Register-ScheduledTask（帳號: $($Credential.UserName)）")) {
        Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
            -Settings $settings -User $Credential.UserName `
            -Password $Credential.GetNetworkCredential().Password `
            -RunLevel Limited -Force | Out-Null
        Write-Host "已建立工作排程器工作: $TaskName（帳號: $($Credential.UserName)）"
    }
} else {
    Write-Warning "未指定 -Credential，將以 SYSTEM 帳號執行（權限較高，正式環境建議改用專用低權限帳號）"
    if ($PSCmdlet.ShouldProcess($TaskName, "Register-ScheduledTask（帳號: SYSTEM）")) {
        $principal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest
        Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
            -Settings $settings -Principal $principal -Force | Out-Null
        Write-Host "已建立工作排程器工作: $TaskName（帳號: SYSTEM）"
    }
}
