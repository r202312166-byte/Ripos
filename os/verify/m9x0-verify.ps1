# m9x0-verify.ps1 -- M9.0 gate: the writable /home RAM disk.
# Boots QEMU (serial-over-TCP + QMP), waits for the M8 shell, then runs
# m9x0gate.py (import m9x0gate) and asserts every marker on serial.

param(
    [string]$BiosImg = "target\bios.img"
    ,[int]$SerialPort = 4570
    ,[int]$QmpPort = 4571
    ,[string]$OutLog = "target\m9x0-serial.log"
)

$ErrorActionPreference = "Stop"
if (-not (Test-Path $BiosImg)) { Write-Error "no $BiosImg"; exit 2 }
if (Test-Path $OutLog) { Remove-Item $OutLog -Force }

$qemu = "C:\Program Files\qemu\qemu-system-x86_64.exe"
Write-Host "M9.0: booting QEMU on $BiosImg (serial tcp $SerialPort, qmp $QmpPort)"
$qemuErr = "target\m9x0-qemu-stderr.log"
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
        Write-Host "M9.0: FAIL - QEMU exited early (code $($qproc.ExitCode))"
        Get-Content $qemuErr -ErrorAction SilentlyContinue | ForEach-Object { Write-Host "  qemu: $_" }
        exit 1
    }
    try {
        $c = New-Object System.Net.Sockets.TcpClient
        $c.Connect("127.0.0.1", $SerialPort)
        $serial = $c
    } catch { Start-Sleep -Milliseconds 500 }
}
if (-not $serial) { Write-Host "M9.0: FAIL - serial tcp never came up"; Stop-Process -Id $qproc.Id -Force -ErrorAction SilentlyContinue; exit 1 }
$sstream = $serial.GetStream()
Write-Host "M9.0: serial connected"

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
    } catch { Write-Host "M9.0: WARN qmp $json failed: $_"; return "" }
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
    } catch { Write-Host "M9.0: WARN typing $s failed: $_" }
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
    Write-Host ("M9.0: step {0}" -f $n)
}

while ($sw.ElapsedMilliseconds -lt $phaseDeadline) {
    if ($sstream.DataAvailable) {
        $n = $sstream.Read($rbuf, 0, 16384)
        $total += [System.Text.Encoding]::ASCII.GetString($rbuf, 0, $n)
    }
    if ($total -match "KERNEL PANIC|Fatal Python error") { $fail = $true; break }
    if ($step -eq 0 -and $total -match "m8: shell ready") {
        Start-Sleep -Milliseconds 500
        Write-Host "M9.0: shell up; running the RAM-disk gate"
        Type-String "import m9x0gate"
        Qmp-Send '{"execute":"human-monitor-command","arguments":{"command-line":"sendkey ret"}}'
        Set-Step 1
    }
    elseif ($step -eq 1 -and $total -match "m9x0 GATE DONE") {
        Write-Host "M9.0: gate script finished; powering off"
        Start-Sleep -Milliseconds 300
        Type-PerChar "exit"
        Qmp-Send '{"execute":"human-monitor-command","arguments":{"command-line":"sendkey ret"}}'
        Set-Step 2
    }
    elseif ($step -eq 2 -and $total -match "Ripos: powering off") { break }
    # step 1 = the RAM-disk gate (must finish); step 2 = the best-effort
    # poweroff, which is a known QEMU i8042 quirk (WARN only, see below)
    if ($script:step -eq 1 -and ($sw.ElapsedMilliseconds -gt ($script:stepTime + 120000))) {
        Write-Host ("M9.0: FAIL - step {0} timed out" -f $script:step)
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
    if ($ok) { Write-Host "M9.0 PASS: $name" -ForegroundColor Green }
    else { Write-Host "M9.0 FAIL: $name (missing: $pattern)" -ForegroundColor Red; $script:pass = $false }
}

Check "home is a directory"                "m9x0 PASS home-mount"
Check "write+read"                          "m9x0 PASS write"
Check "append"                              "m9x0 PASS append"
Check "mkdir"                               "m9x0 PASS mkdir"
Check "listdir"                             "m9x0 PASS listdir"
Check "rename"                              "m9x0 PASS rename"
Check "getsize (stat)"                      "m9x0 PASS getsize"
Check "remove+rmdir"                        "m9x0 PASS remove"
Check "initramfs stays read-only"           "m9x0 PASS ro-initramfs"
Check "exec-from-disk (editor save path)"   "m9x0 PASS exec-from-disk"
Check "truncate"                            "m9x0 PASS truncate"
Check "home in root listing"                "m9x0 PASS home-in-root"
Check "gate completed"                      "m9x0 GATE DONE"

if ($total -match "Ripos: powering off") {
    Write-Host "M9.0 PASS: clean poweroff" -ForegroundColor Green
} else {
    Write-Host "M9.0 WARN: poweroff not observed (QEMU i8042 quirk; not part of the RAM-disk gate)" -ForegroundColor Yellow
}

if (-not $fail -and $pass) {
    Write-Host ""
    Write-Host "M9.0 GATE PASSED: writable /home RAM disk" -ForegroundColor Green
    exit 0
}
Write-Host ""
Write-Host "M9.0 GATE FAILED" -ForegroundColor Red
exit 1
