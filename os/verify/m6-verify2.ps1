# m6-verify2.ps1 -- M6 gate verification: serial-over-TCP capture + QMP key injection.
# The kernel boots to 'm6: ready', then arrow keys move a cursor and letter
# keys paint pixels on the framebuffer; the serial log proves each step.

param(
    [string]$BiosImg = ""
    ,[int]$SerialPort = 4542
    ,[int]$QmpPort = 4543
    ,[string]$OutLog = ""
)

$ErrorActionPreference = "Stop"
if (-not $BiosImg) {
    $cand = Get-ChildItem -Path "target\release\build\os","target\debug\build\os" -Recurse -Filter "bios.img" -ErrorAction SilentlyContinue |
        Sort-Object LastWriteTime -Descending | Select-Object -First 1 -ExpandProperty FullName
    if (-not $cand) { Write-Error "no bios.img found; build first"; exit 2 }
    $BiosImg = $cand
}
if (-not $OutLog) { $OutLog = "target\m6-serial.log" }
if (Test-Path $OutLog) { Remove-Item $OutLog -Force }

$qemu = "C:\Program Files\qemu\qemu-system-x86_64.exe"
Write-Host "M6: booting QEMU on $BiosImg (serial tcp $SerialPort, qmp $QmpPort)"

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

# --- connect to the serial stream --------------------------------------
$serial = $null
$sw = [System.Diagnostics.Stopwatch]::StartNew()
while ($sw.ElapsedMilliseconds -lt 30000 -and -not $serial) {
    try {
        $c = New-Object System.Net.Sockets.TcpClient
        $c.Connect("127.0.0.1", $SerialPort)
        $serial = $c
    } catch { Start-Sleep -Milliseconds 500 }
}
if (-not $serial) { Write-Host "M6: FAIL - serial tcp never came up"; Stop-Job $qjob; Remove-Job $qjob -Force; exit 1 }
$sstream = $serial.GetStream()
Write-Host "M6: serial connected"

# --- QMP helper ---------------------------------------------------------
function Send-Key([string]$key) {
    try {
        $c = New-Object System.Net.Sockets.TcpClient
        $c.Connect("127.0.0.1", $QmpPort)
        $s = $c.GetStream()
        $buf = New-Object byte[] 16384
        Start-Sleep -Milliseconds 150
        $null = $s.Read($buf, 0, 16384)  # greeting
        $json = '{"execute":"qmp_capabilities"}' + [char]10
        $b = [System.Text.Encoding]::ASCII.GetBytes($json)
        $s.Write($b, 0, $b.Length); $s.Flush()
        Start-Sleep -Milliseconds 200
        $null = $s.Read($buf, 0, 16384)
        $json2 = '{"execute":"human-monitor-command","arguments":{"command-line":"sendkey ' + $key + '"}}' + [char]10
        $b2 = [System.Text.Encoding]::ASCII.GetBytes($json2)
        $s.Write($b2, 0, $b2.Length); $s.Flush()
        Start-Sleep -Milliseconds 200
        $c.Close()
    } catch {
        Write-Host "M6: WARN sendkey $key failed: $_"
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

# --- capture serial until m6: ready, then inject keys -------------------
$total = ""
$keysSent = $false
$launched = $false
$keysTime = 0
$rbuf = New-Object byte[] 16384
$phaseDeadline = $sw.ElapsedMilliseconds + 280000
while ($sw.ElapsedMilliseconds -lt $phaseDeadline) {
    if ($sstream.DataAvailable) {
        $n = $sstream.Read($rbuf, 0, 16384)
        $total += [System.Text.Encoding]::ASCII.GetString($rbuf, 0, $n)
    }
    if (-not $launched -and $total -match "m8: shell ready") {
        Write-Host "M6: shell ready; launching the M6 demo"
        Start-Sleep -Milliseconds 800
        Type-Line "import m6"
        $launched = $true
    }
    if (-not $keysSent -and $total -match "m6: ready") {
        Write-Host "M6: boot reached 'm6: ready'; injecting keys"
        foreach ($k in @("left", "up", "right", "down", "left", "a", "b", "c", "spc")) {
            Send-Key $k
            Start-Sleep -Milliseconds 350
        }
        $keysSent = $true
        $keysTime = $sw.ElapsedMilliseconds
    }
    if ($keysSent -and ($sw.ElapsedMilliseconds -gt ($keysTime + 10000) -or $total -match "from ' '")) { break }
    if ($total -match "KERNEL PANIC|Fatal Python error") { break }
    Start-Sleep -Milliseconds 150
}

# --- collect a few more seconds after the keys --------------------------
$end = $sw.ElapsedMilliseconds + 6000
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
($total -split "`n") | Select-Object -Last 35 | ForEach-Object { Write-Host $_ }

# --- verify the M6 gate -------------------------------------------------
$script:pass = $true
function Check([string]$name, [string]$pattern) {
    if ($total -match $pattern) {
        Write-Host "M6 PASS: $name" -ForegroundColor Green
    } else {
        Write-Host "M6 FAIL: $name (missing: $pattern)" -ForegroundColor Red
        $script:pass = $false
    }
}

Check "framebuffer geometry logged"     "m6: fb \d+x\d+ stride=\d+ bpp=\d+ fmt=\d+"
Check "keyboard driver ready"           "m6: keyboard driver ready"
Check "pixel readback (live memory)"    "m6: pixel\(4,4\)=\(255, 0, 0\) pixel\(5,4\)=\(0, 255, 0\) pixel\(6,4\)=\(0, 0, 255\)"
# The demo runs at the boot resolution (1920x1080): cursor starts at the
# centre (960,540) and moves by CURSOR_SIZE=8 per arrow key.
Check "cursor moved left"               "m6: cursor at \(952,540\)"
Check "cursor moved up"                  "m6: cursor at \(952,532\)"
Check "cursor moved right"               "m6: cursor at \(960,532\)"
Check "cursor moved down"                "m6: cursor at \(960,540\)"
Check "letter key painted a pixel"      "m6: painted pixel at \(956,544\) from 'a'"
Check "space key painted a pixel"       "m6: painted pixel at \(956,544\) from ' '"

if ($script:pass) {
    Write-Host ""
    Write-Host "M6 GATE PASSED: keystrokes move pixels on the framebuffer" -ForegroundColor Green
    exit 0
}
Write-Host ""
Write-Host "M6 GATE FAILED" -ForegroundColor Red
exit 1
