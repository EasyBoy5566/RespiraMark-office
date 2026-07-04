# soak_monitor.ps1 - soak test resource monitor (dev tool, Windows only)
# ========================================================================
# Logs memory/CPU of all python processes to a CSV every 60 seconds,
# for long-run stability (soak) testing. Open the CSV in Excel afterwards
# and chart memory_MB over time.
#
# Usage (from the respiramark-office folder):
#   powershell -ExecutionPolicy Bypass -File tools\soak_monitor.ps1
# Stop: Ctrl+C
#
# How to read the results:
#   memory_MB rises then flattens  -> healthy
#   memory_MB keeps climbing       -> memory leak
#   cpu_total_sec is cumulative CPU seconds; +3 sec per minute ~= 5% CPU

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
