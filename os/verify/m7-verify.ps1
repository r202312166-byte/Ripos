# m7-verify.ps1 -- M7 gate: overlapping windows demo.
# Boots Ripos to the Python window manager, then injects keys via QMP:
#   tab     -> cycle focus
#   a b c   -> terminal typing
#   enter   -> run terminal line
#   left/right -> move the focused window
#   ctrl-l  -> switch language en <-> zh
# The serial log proves each step.

param(
    [string]$BiosImg = ""
    ,[int]$SerialPort = 4550
    ,[int]$QmpPort = 4551
    ,[string]$OutLog = ""
)

$ErrorActionPreference = "Stop"
if (-not $BiosImg) {
    $cand = Get-ChildItem -Path "target\release\build\os","target\debug\build\os" -Recurse -Filter "bios.img" -ErrorAction SilentlyContinue |
        Sort-Object LastWriteTime -Descending | Select-Object -First 1 -ExpandProperty FullName
    if (-not $cand) { Write-Error "no bios.img found; build first"; exit 2 }
    $BiosImg = $cand
}
if (-not $OutLog) { $OutLog = "target\m7-serial.log" }
if (Test-Path $OutLog) { Remove-Item $OutLog -Force }

$qemu = "C:\Program Files\qemu\qemu-system-x86_64.exe"
Write-Host "M7: booting QEMU on $BiosImg (serial tcp $SerialPort, qmp $QmpPort)"

$qjob = Start-Job -ScriptBlock {
    param($qemu, $BiosImg, $SerialPort, $QmpPort)
    $a = @(
        "-drive", "format=raw,file=$BiosImg",
        "-display", "none",
        "-m", "1G",
        "-no-reboot",
        "-accel", "tcg,thread=multi",
        "-serial", "tcp:127.0.0.1:$SerialPort,server,nowait",
        "-qmp", "tcp:127.0.0.1:$QmpPort,server,nowait"
    )
    & $qemu @a 2>&1
} -ArgumentList $qemu, $BiosImg, $SerialPort, $QmpPort

# connect to the serial stream
$serial = $null
$sw = [System.Diagnostics.Stopwatch]::StartNew()
while ($sw.ElapsedMilliseconds -lt 30000 -and -not $serial) {
    try {
        $c = New-Object System.Net.Sockets.TcpClient
        $c.Connect("127.0.0.1", $SerialPort)
        $serial = $c
    } catch { Start-Sleep -Milliseconds 500 }
}
if (-not $serial) { Write-Host "M7: FAIL - serial tcp never came up"; Stop-Job $qjob; Remove-Job $qjob -Force; exit 1 }
$sstream = $serial.GetStream()
Write-Host "M7: serial connected"

# QMP helper
function Send-Key([string]$key) {
    try {
        $c = New-Object System.Net.Sockets.TcpClient
        $c.Connect("127.0.0.1", $QmpPort)
        $s = $c.GetStream()
        $buf = New-Object byte[] 16384
        Start-Sleep -Milliseconds 150
        $null = $s.Read($buf, 0, 16384)
        $b = [System.Text.Encoding]::ASCII.GetBytes('{"execute":"qmp_capabilities"}' + [char]10)
        $s.Write($b, 0, $b.Length); $s.Flush()
        Start-Sleep -Milliseconds 200
        $null = $s.Read($buf, 0, 16384)
        $cmd = '{"execute":"human-monitor-command","arguments":{"command-line":"sendkey ' + $key + '"}}' + [char]10
        $b2 = [System.Text.Encoding]::ASCII.GetBytes($cmd)
        $s.Write($b2, 0, $b2.Length); $s.Flush()
        Start-Sleep -Milliseconds 200
        $c.Close()
    } catch {
        Write-Host "M7: WARN sendkey $key failed: $_"
    }
}

function Type-Line([string]$s) {
    foreach ($ch in $s.ToCharArray()) {
        $k = if ($ch -eq ' ') { 'spc' } else { [string]$ch }
        Send-Key $k
        Start-Sleep -Milliseconds 120
    }
    Send-Key "ret"
}

# capture until ready, then drive the demo
$total = ""
$sent = $false
$launched = $false
$rbuf = New-Object byte[] 16384
$phaseDeadline = $sw.ElapsedMilliseconds + 600000
while ($sw.ElapsedMilliseconds -lt $phaseDeadline) {
    if ($sstream.DataAvailable) {
        $n = $sstream.Read($rbuf, 0, 16384)
        $total += [System.Text.Encoding]::ASCII.GetString($rbuf, 0, $n)
    }
    if (-not $launched -and $total -match "m8: shell ready") {
        Write-Host "M7: shell ready; launching the M7 demo"
        Start-Sleep -Milliseconds 800
        Type-Line "import m7"
        $launched = $true
    }
    if (-not $sent -and $total -match "m7: gate: overlapping windows demo ready") {
        Write-Host "M7: GUI ready; driving the demo"
        # focus starts on the terminal (index 1).  Cycle to sys, move it,
        # return to the terminal, type a line, switch languages, type again.
        $seq = @("tab", "tab", "left", "right", "tab", "a", "b", "c", "ret", "ctrl-l", "ctrl-l", "x", "ret")
        foreach ($k in $seq) {
            Send-Key $k
            Start-Sleep -Milliseconds 3000
        }
        $sent = $true
    }
    if ($sent -and ($total -match "m7: lang=en" -and $total -match "m7: term line 1: abc" -and $total -match "m7: term line 2")) { break }
    if ($sent -and $sw.ElapsedMilliseconds -gt ($phaseDeadline - 30000)) { break }
    if ($total -match "KERNEL PANIC|Fatal Python error") { break }
    Start-Sleep -Milliseconds 150
}
# a little more soak
$end = $sw.ElapsedMilliseconds + 10000
while ($sw.ElapsedMilliseconds -lt $end) {
    if ($sstream.DataAvailable) {
        $n = $sstream.Read($rbuf, 0, 16384)
        $total += [System.Text.Encoding]::ASCII.GetString($rbuf, 0, $n)
    } else { Start-Sleep -Milliseconds 100 }
}
$serial.Close()
Stop-Job $qjob -ErrorAction SilentlyContinue
Remove-Job $qjob -Force -ErrorAction SilentlyContinue

$total | Set-Content $OutLog -Encoding utf8
Write-Host ""
Write-Host "===== serial log tail ====="
($total -split "`n") | Select-Object -Last 30 | ForEach-Object { Write-Host $_ }

$script:pass = $true
function Check([string]$name, [string]$pattern) {
    if ($total -match $pattern) {
        Write-Host "M7 PASS: $name" -ForegroundColor Green
    } else {
        Write-Host "M7 FAIL: $name (missing: $pattern)" -ForegroundColor Red
        $script:pass = $false
    }
}

Check "GUI booted"                 "m7: desktop \d+x\d+ focus=\d+ windows=3"
Check "windows created"            "m7: windows created: sys term lang"
Check "titlebar rendered (pixel)" "m7: titlebar pixel=\(\d+, \d+, \d+\)"
Check "gate marker"                "m7: gate: overlapping windows demo ready"
Check "focus cycled"               "m7: focus=\d+ \(win.(system|terminal|language)\)"
Check "terminal typing"            "m7: term line 1: abc"
Check "window moved (sys)"         "m7: move win 0 -> \(\d+,\d+\)"
Check "language switched to zh"    "m7: lang=zh"
Check "language switched back en"  "m7: lang=en"
Check "terminal works after lang toggle" "m7: term line 2: x"

if ($script:pass) {
    Write-Host ""
    Write-Host "M7 GATE PASSED: overlapping windows demo (focus, terminal, en/zh language switch)" -ForegroundColor Green
    exit 0
}
Write-Host ""
Write-Host "M7 GATE FAILED" -ForegroundColor Red
exit 1
