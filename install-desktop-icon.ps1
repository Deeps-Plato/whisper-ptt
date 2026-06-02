# Creates a "Whisper PTT" shortcut on the Desktop that launches ptt.py headless.
# Run via install-desktop-icon.bat (double-click) or: powershell -ExecutionPolicy Bypass -File install-desktop-icon.ps1
$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$PttPath   = Join-Path $ScriptDir "ptt.py"
$IconPath  = Join-Path $ScriptDir "whisper-ptt.ico"

if (-not (Test-Path $PttPath)) {
    throw "ptt.py not found next to this script ($ScriptDir)"
}

# Resolve pythonw.exe (no console window) with a fallback to python.exe
$Pythonw = (Get-Command pythonw.exe -ErrorAction SilentlyContinue).Source
if (-not $Pythonw) {
    $Pythonw = (Get-Command python.exe -ErrorAction SilentlyContinue).Source
}
if (-not $Pythonw) {
    throw "Neither pythonw.exe nor python.exe found on PATH"
}

# Generate the brand icon (white mic on green circle); fall back to a stock icon
$Python = (Get-Command python.exe -ErrorAction SilentlyContinue).Source
if ($Python) {
    try { & $Python (Join-Path $ScriptDir "make_icon.py") | Out-Null } catch { }
}
if (-not (Test-Path $IconPath)) {
    Write-Warning "Could not generate whisper-ptt.ico; using a stock microphone icon"
    $IconPath = "$env:SystemRoot\System32\SndVolSSO.dll,0"
}

$Desktop  = [Environment]::GetFolderPath("Desktop")
$LinkPath = Join-Path $Desktop "Whisper PTT.lnk"

$Shell    = New-Object -ComObject WScript.Shell
$Shortcut = $Shell.CreateShortcut($LinkPath)
$Shortcut.TargetPath       = $Pythonw
$Shortcut.Arguments        = "`"$PttPath`""
$Shortcut.WorkingDirectory = $ScriptDir
$Shortcut.IconLocation     = $IconPath
$Shortcut.WindowStyle      = 7   # minimized; pythonw shows no window anyway
$Shortcut.Description       = "Start Whisper push-to-talk dictation"
$Shortcut.Save()

Write-Host "Created desktop shortcut: $LinkPath"
Write-Host "  -> $Pythonw `"$PttPath`""
