# m8-verify.ps1 -- M8 gate: full boot-to-shell demo.
# Boots Ripos to the interactive Python REPL (the OS command line), then
# injects keys via QMP and proves the transcript on the serial console:
#   "1 + 1"          -> 2            (bare-expression printing)
#   "x = 21" / "x * 2" -> 42         (persistent namespace)
#   "print(40 + 2)"  -> 42           (statements, stdout)
#   "import kern" / "kern.tick()" -> uptime ms
#   "help"           -> usage
#   "exit"           -> powers the machine off

param(
    [string]$BiosImg = ""
    ,[int]$SerialPort = 4560
    ,[int]$QmpPort = 4561
    ,[string]$OutLog = ""
)

$ErrorActionPreference = "Stop"
if (-not $BiosImg) {
    $cand = Get-ChildItem -Path "target\release\build\os","target\debug\build\os" -Recurse -Filter "bios.img" -ErrorAction SilentlyContinue |
        Sort-Object LastWriteTime -Descending | Select-Object -First 1 -ExpandProperty FullName
    if (-not $cand) { Write-Error "no bios.img found; build first"; exit 2 }
    $BiosImg = $cand
}
if (-not $OutLog) { $OutLog = "target\m8-serial.log" }
if (Test-Path $OutLog) { Remove-Item $OutLog -Force }

$qemu = "C:\Program Files\qemu\qemu-system-x86_64.exe"
Write-Host "M8: booting QEMU on $BiosImg (serial tcp $SerialPort, qmp $QmpPort)"

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
if (-not $serial) { Write-Host "M8: FAIL - serial tcp never came up"; Stop-Job $qjob; Remove-Job $qjob -Force; exit 1 }
$sstream = $serial.GetStream()
Write-Host "M8: serial connected"

# QMP helper (sendkey with shift support for symbols: ( ) + * etc.)
function Send-Key([string]$key) {
    try {
        $c = New-Object System.Net.Sockets.TcpClient
        $c.Connect("127.0.0.1", $QmpPort)
        $s = $c.GetStream()
        $buf = New-Object byte[] 16384
        Start-Sleep -Milliseconds 120
        $null = $s.Read($buf, 0, 16384)
        $b = [System.Text.Encoding]::ASCII.GetBytes('{"execute":"qmp_capabilities"}' + [char]10)
        $s.Write($b, 0, $b.Length); $s.Flush()
        Start-Sleep -Milliseconds 150
        $null = $s.Read($buf, 0, 16384)
        $cmd = '{"execute":"human-monitor-command","arguments":{"command-line":"sendkey ' + $key + '"}}' + [char]10
        $b2 = [System.Text.Encoding]::ASCII.GetBytes($cmd)
        $s.Write($b2, 0, $b2.Length); $s.Flush()
        Start-Sleep -Milliseconds 120
        $c.Close()
    } catch {
        Write-Host "M8: WARN sendkey $key failed: $_"
    }
}

# type a whole line: each token is a QMP sendkey argument
function Type-Line([string[]]$keys) {
    foreach ($k in $keys) {
        Send-Key $k
        Start-Sleep -Milliseconds 250
    }
    Send-Key "ret"
    Start-Sleep -Milliseconds 1500
}

$total = ""
$sent = $false
$rbuf = New-Object byte[] 16384
$phaseDeadline = $sw.ElapsedMilliseconds + 600000
while ($sw.ElapsedMilliseconds -lt $phaseDeadline) {
    if ($sstream.DataAvailable) {
        $n = $sstream.Read($rbuf, 0, 16384)
        $total += [System.Text.Encoding]::ASCII.GetString($rbuf, 0, $n)
    }
    if (-not $sent -and $total -match "m8: shell ready") {
        Write-Host "M8: shell ready; driving the demo"
        Type-Line @("1", "spc", "shift-equal", "spc", "1")
        Type-Line @("x", "spc", "equal", "spc", "2", "1")
        Type-Line @("x", "spc", "shift-8", "spc", "2")
        Type-Line @("p", "r", "i", "n", "t", "shift-9", "4", "0", "spc", "shift-equal", "spc", "2", "shift-0")
        Type-Line @("i", "m", "p", "o", "r", "t", "spc", "k", "e", "r", "n")
        Type-Line @("k", "e", "r", "n", "dot", "t", "i", "c", "k", "shift-9", "shift-0")
        Type-Line @("h", "e", "l", "p")
        Type-Line @("c", "l", "e", "a", "r")
        Type-Line @("e", "x", "i", "t")
        $sent = $true
    }
    if ($sent -and $total -match "Ripos: powering off") { break }
    if ($sent -and $sw.ElapsedMilliseconds -gt ($phaseDeadline - 30000)) { break }
    if ($total -match "KERNEL PANIC|Fatal Python error") { break }
    Start-Sleep -Milliseconds 150
}
# a little more soak
$end = $sw.ElapsedMilliseconds + 5000
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
($total -split "`n") | Select-Object -Last 40 | ForEach-Object { Write-Host $_ }

$script:pass = $true
function Check([string]$name, [string]$pattern) {
    if ($total -match $pattern) {
        Write-Host "M8 PASS: $name" -ForegroundColor Green
    } else {
        Write-Host "M8 FAIL: $name (missing: $pattern)" -ForegroundColor Red
        $script:pass = $false
    }
}

Check "shell booted"                "m8: shell ready"
Check "prompt shown"                ">>> "
Check "bare expression prints"      ">>> 1 \+ 1[\r\n]+2[\r\n]+>>> "
Check "namespace persists (x=21)"   ">>> x \* 2[\r\n]+42[\r\n]+>>> "
Check "statement stdout (print)"    ">>> print\(40 \+ 2\)[\r\n]+42[\r\n]+>>> "
Check "import worked"               ">>> import kern[\r\n]+>>> "
Check "kern.tick() returns ms"      ">>> kern\.tick\(\)[\r\n]+[0-9]{3,}[\r\n]+>>> "
Check "help command"                "Ripos shell commands"
Check "clear command"               ">>> clear[\r\n]+>>> "
Check "exit powers off"             "Ripos: powering off"

if ($script:pass) {
    Write-Host ""
    Write-Host "M8 GATE PASSED: full boot-to-shell demo (interactive Python REPL)" -ForegroundColor Green
    exit 0
}
Write-Host ""
Write-Host "M8 GATE FAILED" -ForegroundColor Red
exit 1
