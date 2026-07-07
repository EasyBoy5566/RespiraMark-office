# soak_monitor.ps1 — 長期資源監控（開發/驗收用，僅限 Windows；IMPROVEMENT_PLAN.md W-207/W-401）
# ================================================================================
# 每 60 秒把所有 python 程序的記憶體/CPU 記到一個 CSV，供長時間穩定性
# （soak test）驗證用。事後用 Excel 打開，把 memory_MB 畫成折線圖看趨勢。
#
# 用法（在 respiramark-office 資料夾執行）：
#   powershell -ExecutionPolicy Bypass -File tools\soak_monitor.ps1
# 停止：Ctrl+C
#
# 判讀標準：
#   memory_MB 先上升後打平        → 正常（初期填快取/連線很正常）
#   memory_MB 持續緩慢往上爬升，  → 需回報：對應 Phase 4 W-401「72 小時內
#     累計 72 小時成長超過 5%       成長 <5%」的驗收標準；若確認是持續趨勢
#                                    （非單次尖峰），視為疑似記憶體洩漏
#   memory_MB 突然跳一大截後打平  → 通常是新裝置/新觀看端連入，正常現象，
#                                    非趨勢性成長不用擔心
#   cpu_total_sec 是累計 CPU 秒數；平均每分鐘 +3 秒 ≈ 5% CPU 使用率

param(
    [int]$IntervalSec = 60,
    [string]$OutFile = "$HOME\Desktop\resource_log.csv"
)

"time,pid,memory_MB,cpu_total_sec" | Out-File $OutFile -Encoding utf8
Write-Host "Logging every $IntervalSec sec -> $OutFile  (press Ctrl+C to stop)"

while ($true) {
    $ts = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    $procs = @(Get-Process python* -ErrorAction SilentlyContinue)
    foreach ($p in $procs) {
        $mem = [math]::Round($p.WorkingSet64 / 1MB, 1)
        $cpu = ""
        if ($null -ne $p.CPU) { $cpu = [math]::Round($p.CPU, 1) }
        "$ts,$($p.Id),$mem,$cpu" | Out-File $OutFile -Append -Encoding utf8
    }
    if ($procs.Count -gt 0) {
        Write-Host "$ts  logged $($procs.Count) python process(es)"
    } else {
        Write-Host "$ts  no python process found (is the server running?)"
    }
    Start-Sleep -Seconds $IntervalSec
}
