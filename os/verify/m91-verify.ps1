# m91-verify.ps1 -- M9.1 gate: zlib/_bz2/_lzma/_elementtree format modules.
# Boots QEMU (serial-over-TCP + QMP), waits for the M8 shell, then runs
# m91gate.py (import m91gate) and asserts every marker on serial.

param(
    [string]$BiosImg = "target\bios.img"
    ,[int]$SerialPort = 4580
    ,[int]$QmpPort = 4581
    ,[string]$OutLog = "target\m91-serial.log"
)

$ErrorActionPreference = "Stop"
if (-not (Test-Path $BiosImg)) { Write-Error "no $BiosImg"; exit 2 }
if (Test-Path $OutLog) { Remove-Item $OutLog -Force }

$qemu = "C:\Program Files\qemu\qemu-system-x86_64.exe"
Write-Host "M9.1: booting QEMU on $BiosImg (serial tcp $SerialPort, qmp $QmpPort)"
$qemuErr = "target\m91-qemu-stderr.log"
if (Test-Path $qemuErr) { Remove-Item $qemuErr -Force }
$qargs = @(
    "-drive", "format=raw,file=$BiosImg",
    "-display", "none",
    "-m", "1G",
    "-no-reboot",
    "-accel", "tcg,thread=multi",
    "-device", "isa-debug-exit,iobase=0xf4,iosize=0x04",
    "-serial", "tcp:127.0.0.1:$SerialPort,server,nowait",
    "-qmp", "tcp:127.0.0.1:$QmpPort,server,nowait"
)
$qproc = Start-Process -FilePath $qemu -ArgumentList $qargs -PassThru -NoNewWindow -RedirectStandardError $qemuErr

$serial = $null
$sw = [System.Diagnostics.Stopwatch]::StartNew()
while ($sw.ElapsedMilliseconds -lt 30000 -and -not $serial) {
    if ($qproc.HasExited) {
        Write-Host "M9.1: FAIL - QEMU exited early (code $($qproc.ExitCode))"
        Get-Content $qemuErr -ErrorAction SilentlyContinue | ForEach-Object { Write-Host "  qemu: $_" }
        exit 1
    }
    try {
        $c = New-Object System.Net.Sockets.TcpClient
        $c.Connect("127.0.0.1", $SerialPort)
        $serial = $c
    } catch { Start-Sleep -Milliseconds 500 }
}
if (-not $serial) { Write-Host "M9.1: FAIL - serial tcp never came up"; Stop-Process -Id $qproc.Id -Force -ErrorAction SilentlyContinue; exit 1 }
$sstream = $serial.GetStream()
Write-Host "M9.1: serial connected"

function Qmp-Send([string]$json) {
    try {
        $c = New-Object System.Net.Sockets.TcpClient
        $c.Connect("127.0.0.1", $QmpPort)
        $s = $c.GetStream()
        $buf = New-Object byte[] 16384
        Start-Sleep -Milliseconds 100
        $null = $s.Read($buf, 0, 16384)
        $b = [System.Text.Encoding]::ASCII.GetBytes('{"execute":"qmp_capabilities"}' + [char]10)
        $s.Write($b, 0, $b.Length); $s.Flush()
        Start-Sleep -Milliseconds 100
        $null = $s.Read($buf, 0, 16384)
        $b2 = [System.Text.Encoding]::ASCII.GetBytes($json + [char]10)
        $s.Write($b2, 0, $b2.Length); $s.Flush()
        Start-Sleep -Milliseconds 120
        $c.Close()
        return ""
    } catch { Write-Host "M9.1: WARN qmp $json failed: $_"; return "" }
}

function Type-String([string]$s) {
    $map = @{ " " = "spc" }
    $keys = @()
    foreach ($ch in $s.ToCharArray()) {
        if ($map.ContainsKey([string]$ch)) { $keys += $map[[string]$ch] }
        else { $keys += [string]$ch }
    }
    try {
        $c = New-Object System.Net.Sockets.TcpClient
        $c.Connect("127.0.0.1", $QmpPort)
        $st = $c.GetStream()
        $buf = New-Object byte[] 16384
        Start-Sleep -Milliseconds 100
        $null = $st.Read($buf, 0, 16384)
        $b = [System.Text.Encoding]::ASCII.GetBytes('{"execute":"qmp_capabilities"}' + [char]10)
        $st.Write($b, 0, $b.Length); $st.Flush()
        Start-Sleep -Milliseconds 100
        $null = $st.Read($buf, 0, 16384)
        foreach ($k in $keys) {
            $j = '{"execute":"human-monitor-command","arguments":{"command-line":"sendkey ' + $k + '"}}'
            $b2 = [System.Text.Encoding]::ASCII.GetBytes($j + [char]10)
            $st.Write($b2, 0, $b2.Length); $st.Flush()
            Start-Sleep -Milliseconds 150
        }
        Start-Sleep -Milliseconds 100
        $c.Close()
    } catch { Write-Host "M9.1: WARN typing $s failed: $_" }
}

function Type-PerChar([string]$s) {
    foreach ($ch in $s.ToCharArray()) {
        Qmp-Send ('{"execute":"human-monitor-command","arguments":{"command-line":"sendkey ' + $ch + '"}}')
        Start-Sleep -Milliseconds 130
    }
}

$total = ""
$step = 0
$stepTime = 0
$rbuf = New-Object byte[] 16384
$phaseDeadline = $sw.ElapsedMilliseconds + 420000
$fail = $false

function Set-Step([int]$n) {
    $script:step = $n
    $script:stepTime = $script:sw.ElapsedMilliseconds
    Write-Host ("M9.1: step {0}" -f $n)
}

while ($sw.ElapsedMilliseconds -lt $phaseDeadline) {
    if ($sstream.DataAvailable) {
        $n = $sstream.Read($rbuf, 0, 16384)
        $total += [System.Text.Encoding]::ASCII.GetString($rbuf, 0, $n)
    }
    if ($total -match "KERNEL PANIC|Fatal Python error") { $fail = $true; break }
    if ($step -eq 0 -and $total -match "m8: shell ready") {
        Start-Sleep -Milliseconds 500
        Write-Host "M9.1: shell up; running the format-modules gate"
        Type-String "import m91gate"
        Qmp-Send '{"execute":"human-monitor-command","arguments":{"command-line":"sendkey ret"}}'
        Set-Step 1
    }
    elseif ($step -eq 1 -and $total -match "m91 GATE DONE") {
        Write-Host "M9.1: gate script finished; powering off"
        Start-Sleep -Milliseconds 300
        Type-PerChar "exit"
        Qmp-Send '{"execute":"human-monitor-command","arguments":{"command-line":"sendkey ret"}}'
        Set-Step 2
    }
    elseif ($step -eq 2 -and $total -match "Ripos: powering off") { break }
    # step 1 = the format-modules gate (must finish); step 2 = the
    # best-effort poweroff, which is a known QEMU i8042 quirk (WARN only)
    if ($script:step -eq 1 -and ($sw.ElapsedMilliseconds -gt ($script:stepTime + 120000))) {
        Write-Host ("M9.1: FAIL - step 1 timed out" -f $script:step)
        $fail = $true; break
    }
    Start-Sleep -Milliseconds 120
}

$serial.Close()
Stop-Process -Id $qproc.Id -Force -ErrorAction SilentlyContinue
$total | Set-Content $OutLog -Encoding utf8

Write-Host ""
Write-Host "===== serial log tail ====="
($total -split "`n") | Select-Object -Last 30 | ForEach-Object { Write-Host $_ }

$pass = $true
function Check([string]$name, [string]$pattern) {
    try { $ok = $total -match $pattern } catch { $ok = $false }
    if ($ok) { Write-Host "M9.1 PASS: $name" -ForegroundColor Green }
    else { Write-Host "M9.1 FAIL: $name (missing: $pattern)" -ForegroundColor Red; $script:pass = $false }
}

Check "zlib import+version"              "m91 PASS zlib import"
Check "zlib roundtrip"                     "m91 PASS zlib roundtrip"
Check "zlib crc32"                         "m91 PASS zlib crc32"
Check "gzip roundtrip"                     "m91 PASS gzip roundtrip"
Check "bz2 roundtrip"                      "m91 PASS bz2 roundtrip"
Check "lzma roundtrip"                     "m91 PASS lzma roundtrip"
Check "zipfile create"                     "m91 PASS zipfile create"
Check "zipfile read (DEFLATED)"            "m91 PASS zipfile read"
Check "tar.gz roundtrip"                   "m91 PASS tar.gz roundtrip"
Check "_elementtree (C expat)"             "m91 PASS _elementtree import"
Check "elementtree parse"                  "m91 PASS elementtree parse"
Check "shutil copy"                        "m91 PASS shutil copy"
Check "gate completed"                      "m91 GATE DONE"

if ($total -match "Ripos: powering off") {
    Write-Host "M9.1 PASS: clean poweroff" -ForegroundColor Green
} else {
    Write-Host "M9.1 WARN: poweroff not observed (QEMU i8042 quirk; not part of the format-modules gate)" -ForegroundColor Yellow
}

if (-not $fail -and $pass) {
    Write-Host ""
    Write-Host "M9.1 GATE PASSED: zlib/_bz2/_lzma/_elementtree unlock gzip/tar/bz2/lzma/zipfile-DEFLATED" -ForegroundColor Green
    exit 0
}
Write-Host ""
Write-Host "M9.1 GATE FAILED" -ForegroundColor Red
exit 1