# whisper-ptt watchdog: auto-start at logon + self-heal.
# Launches ptt.py (venv pythonw) if no whisper-ptt python process is running,
# then re-checks every 5 minutes forever. Put a shortcut to this script in
# shell:startup (powershell -WindowStyle Hidden -File watchdog.ps1).
# Paths are derived from the script location, so it works from any clone.

$repo = $PSScriptRoot
$pyw  = Join-Path $repo ".venv\Scripts\pythonw.exe"
if (-not (Test-Path $pyw)) { $pyw = "pythonw.exe" }   # no venv: use PATH python
$log  = Join-Path $repo "watchdog.log"

function Test-Ptt {
    $p = Get-Process pythonw -ErrorAction SilentlyContinue |
         Where-Object { try { $_.Path -like "*whisper-ptt*" } catch { $false } }
    return [bool]$p
}

while ($true) {
    if (-not (Test-Ptt)) {
        Start-Process -FilePath $pyw -ArgumentList "ptt.py" -WorkingDirectory $repo
        $stamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
        Add-Content -Path $log -Value "$stamp started ptt.py" -Encoding utf8
    }
    Start-Sleep -Seconds 300
}
