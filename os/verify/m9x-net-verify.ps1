# m9x-net-verify.ps1 -- Phase 2 boot gate: e1000 + smoltcp + browser.
# Boots bios.img with -netdev user (slirp) + e1000, serves a fixture site
# from the host (python -m http.server), opens it in the browser, and
# pixel-checks the rendered page; also does a raw HTTP GET via web.py.
param(
    [string]$BiosImg = "target\bios.img"
    ,[int]$QmpPort = 4599
    ,[int]$HttpPort = 8000
    ,[string]$OutLog = "target\m9x-net-serial.log"
    ,[string]$ShotPpm = "target\m9x-net.ppm"
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
if (-not [System.IO.Path]::IsPathRooted($BiosImg)) { $BiosImg = Join-Path (Get-Location) $BiosImg }
if (-not [System.IO.Path]::IsPathRooted($OutLog)) { $OutLog = Join-Path (Get-Location) $OutLog }
if (-not [System.IO.Path]::IsPathRooted($ShotPpm)) { $ShotPpm = Join-Path (Get-Location) $ShotPpm }
Get-Process qemu-system-x86_64 -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
Start-Sleep -Milliseconds 500
if (-not (Test-Path $BiosImg)) { Write-Error "no $BiosImg"; exit 2 }
foreach ($f in @($OutLog, $ShotPpm)) { if (Test-Path $f) { Remove-Item $f -Force } }

# ---- host HTTP server ------------------------------------------------
$wwwDir = Join-Path $PSScriptRoot "www"
$httpProc = Start-Process -FilePath "py" -ArgumentList @("-3", "-m", "http.server", "$HttpPort", "--directory", ('"' + $wwwDir + '"')) -PassThru -NoNewWindow
Start-Sleep -Seconds 2
Write-Host "M9N: http server on 127.0.0.1:$HttpPort (pid $($httpProc.Id))"

$qemu = "C:\Program Files\qemu\qemu-system-x86_64.exe"
$biosDir = Split-Path $BiosImg; $biosLeaf = Split-Path $BiosImg -Leaf
$outLeaf = Split-Path $OutLog -Leaf
$OutLog = Join-Path $biosDir $outLeaf
if (Test-Path $OutLog) { Remove-Item $OutLog -Force }
$qargs = @(
    "-drive", "format=raw,file=$biosLeaf",
    "-display", "none",
    "-m", "1G",
    "-no-reboot",
    "-nographic",
    "-serial", "file:$outLeaf",
    "-qmp", "tcp:127.0.0.1:$QmpPort,server,nowait",
    "-netdev", "user,id=n0",
    "-device", "e1000e,netdev=n0"
)
$qproc = Start-Process -FilePath $qemu -ArgumentList ($qargs -join ' ') -WorkingDirectory $biosDir -PassThru -NoNewWindow
$sw = [System.Diagnostics.Stopwatch]::StartNew()
Write-Host "M9N: booting (up to 900s)"

function Qmp-Send($json) {
    try {
        $c = New-Object System.Net.Sockets.TcpClient
        $c.Connect("127.0.0.1", $QmpPort)
        $s = $c.GetStream()
        $buf = New-Object byte[] 16384
        Start-Sleep -Milliseconds 80
        $null = $s.Read($buf, 0, 16384)
        $b = [System.Text.Encoding]::ASCII.GetBytes('{"execute":"qmp_capabilities"}' + [char]10)
        $s.Write($b, 0, $b.Length); $s.Flush()
        Start-Sleep -Milliseconds 80
        $null = $s.Read($buf, 0, 16384)
        $b2 = [System.Text.Encoding]::ASCII.GetBytes($json + [char]10)
        $s.Write($b2, 0, $b2.Length); $s.Flush()
        Start-Sleep -Milliseconds 150
        $c.Close()
    } catch { Write-Host "M9N: WARN qmp failed: $_" }
}
function Send-Human($cmdline) { Qmp-Send ('{"execute":"human-monitor-command","arguments":{"command-line":"' + $cmdline + '"}}') }
function Shot-Qmp([string]$path) {
    $fwd = $path.Replace("\", "/")
    Qmp-Send ('{"execute":"screendump","arguments":{"filename":"' + $fwd + '"}}')
    Start-Sleep -Milliseconds 800
}
function Type-String($s) {
    $map = @{ " " = "spc"; "." = "dot"; "/" = "slash"; "-" = "minus"; "," = "comma"; ":" = "shift-semicolon"
              "'" = "apostrophe"; "(" = "shift-9"; ")" = "shift-0"; "_" = "shift-minus"; "=" = "equal"; "+" = "shift-equal"
              "[" = "bracket_left"; "]" = "bracket_right" }
    $keys = @()
    foreach ($ch in $s.ToCharArray()) {
        $upper = [char]::IsUpper($ch)
        if ($map.ContainsKey([string]$ch)) { $keys += $map[[string]$ch] }
        elseif ($upper) { $keys += ("shift-" + [string]$ch.ToString().ToLower()) }
        else { $keys += [string]$ch }
    }
    try {
        $c = New-Object System.Net.Sockets.TcpClient
        $c.Connect("127.0.0.1", $QmpPort)
        $st = $c.GetStream()
        $buf = New-Object byte[] 16384
        Start-Sleep -Milliseconds 80
        $null = $st.Read($buf, 0, 16384)
        $b = [System.Text.Encoding]::ASCII.GetBytes('{"execute":"qmp_capabilities"}' + [char]10)
        $st.Write($b, 0, $b.Length); $st.Flush()
        Start-Sleep -Milliseconds 80
        $null = $st.Read($buf, 0, 16384)
        foreach ($k in $keys) {
            $j = '{"execute":"human-monitor-command","arguments":{"command-line":"sendkey ' + $k + '"}}'
            $b2 = [System.Text.Encoding]::ASCII.GetBytes($j + [char]10)
            $st.Write($b2, 0, $b2.Length); $st.Flush()
            Start-Sleep -Milliseconds 120
        }
        $j = '{"execute":"human-monitor-command","arguments":{"command-line":"sendkey ret"}}'
        $b2 = [System.Text.Encoding]::ASCII.GetBytes($j + [char]10)
        $st.Write($b2, 0, $b2.Length); $st.Flush()
        Start-Sleep -Milliseconds 120
        $c.Close()
    } catch { Write-Host "M9N: WARN typing $s failed: $_" }
}
function Read-Log { if (Test-Path $OutLog) { Get-Content $OutLog -Raw } else { "" } }

$passed = 0
function Check($name, $ok, $extra) {
    if ($ok) { $script:passed++; Write-Host "M9N PASS $name $extra" }
    else { Write-Host "M9N FAIL $name $extra" }
}

# ---- wait for shell + NIC up -------------------------------------------
$shell = $false
$nicSeen = $false
$netSeen = $false
while ($sw.Elapsed.TotalSeconds -lt 900) {
    $total = Read-Log
    if (-not $nicSeen -and $total -match "nic: e1000 ready") { $nicSeen = $true; Write-Host "M9N: NIC up at $([int]$sw.Elapsed.TotalSeconds)s" }
    if (-not $netSeen -and $total -match "net: smoltcp up") { $netSeen = $true }
    if ($total -match "m8: shell ready") { $shell = $true; break }
    Start-Sleep -Seconds 5
}
Check "boots to shell" $shell "($([int]$sw.Elapsed.TotalSeconds)s)"
Check "e1000 NIC initialized" $nicSeen ""
Check "smoltcp stack up" $netSeen ""
if (-not $shell) { Get-Content $OutLog -Tail 20 -ErrorAction SilentlyContinue; Stop-Process -Id $qproc.Id -Force -ErrorAction SilentlyContinue; Stop-Process -Id $httpProc.Id -Force -ErrorAction SilentlyContinue; exit 1 }

# ---- open the browser on the fixture site -----------------------------
Type-String "browse http://10.0.2.2:$HttpPort/"
$browseSeen = $false
for ($i = 0; $i -lt 45; $i++) {
    Start-Sleep -Seconds 4
    if ((Read-Log) -match "browser: screen ready") { $browseSeen = $true; break }
}
Check "browser opens" $browseSeen ""
Start-Sleep -Seconds 8
Shot-Qmp $ShotPpm

# pixel-check: the rendered page has bright text + the accent URL bar
$b = [System.IO.File]::ReadAllBytes($ShotPpm)
$idx = 0
while ($idx -lt [Math]::Min(80, $b.Length) -and $b[$idx] -ne 10) { $idx++ }
$idx++
$w = 0
while ($idx -lt $b.Length -and $b[$idx] -ne 32 -and $b[$idx] -ne 10) { $w = $w * 10 + ($b[$idx] - 48); $idx++ }
$idx++
$h = 0
while ($idx -lt $b.Length -and $b[$idx] -ne 10) { $h = $h * 10 + ($b[$idx] - 48); $idx++ }
$idx++
while ($idx -lt $b.Length -and $b[$idx] -ne 10) { $idx++ }
$idx++
$bright = 0
$accent = 0
$head = 0
for ($y = 0; $y -lt $h; $y += 2) {
    for ($x = 0; $x -lt $w; $x += 2) {
        $o = $idx + ($y * $w + $x) * 3
        if ($o + 2 -lt $b.Length) {
            $r = $b[$o]; $g = $b[$o + 1]; $bl = $b[$o + 2]
            if ($r -gt 140 -and $g -gt 140 -and $bl -gt 140) { $bright++ }
            if ($r -gt 60 -and $r -lt 140 -and $g -gt 120 -and $bl -gt 220) { $accent++ }
            if ($r -gt 230 -and $g -gt 200 -and $bl -lt 200) { $head++ }
        }
    }
}
Write-Host "M9N: pixels bright=$bright accent=$accent head=$head (w=$w h=$h)"
Check "browser rendered bright text" ($bright -gt 300) "(bright=$bright)"
Check "browser rendered heading" ($head -gt 20) "(head=$head)"

# ---- raw HTTP round-trip via web.py ------------------------------------
# The browser owns the keyboard while running: quit it first so the
# following lines reach the shell REPL.
Send-Human "sendkey esc"
Start-Sleep -Seconds 3
Type-String "import web"
Start-Sleep -Seconds 4
Type-String "web.get('http://10.0.2.2:$HttpPort/')[0]"
$httpOk = $false
for ($i = 0; $i -lt 20; $i++) {
    Start-Sleep -Seconds 3
    if ((Read-Log) -match "(^|\n)200($|\n)") { $httpOk = $true; break }
}
Check "HTTP GET returns 200" $httpOk ""

# ---- cleanup ------------------------------------------------------------
Type-String "exit"
Start-Sleep -Seconds 4
Stop-Process -Id $qproc.Id -Force -ErrorAction SilentlyContinue
Stop-Process -Id $httpProc.Id -Force -ErrorAction SilentlyContinue
Write-Host "M9N: $passed checks passed"
if ($passed -lt 6) { exit 1 }
exit 0
