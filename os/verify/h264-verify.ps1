# h264-gate.ps1 -- boot the OS and play testvideo-h264.mp4 (H.264 baseline
# via the _h264 h264bsd module); pixel-check the decoded video area.
param(
    [string]$BiosImg = "target\bios.img",
    [int]$QmpPort = 4703,
    [string]$OutLog = "target\h264-serial.log",
    [string]$ShotPpm = "target\h264-video.ppm"
)
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
if (-not [System.IO.Path]::IsPathRooted($BiosImg)) { $BiosImg = Join-Path (Get-Location) $BiosImg }
if (-not [System.IO.Path]::IsPathRooted($OutLog)) { $OutLog = Join-Path (Get-Location) $OutLog }
if (-not [System.IO.Path]::IsPathRooted($ShotPpm)) { $ShotPpm = Join-Path (Get-Location) $ShotPpm }
Get-Process qemu-system-x86_64 -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
Start-Sleep -Milliseconds 500
foreach ($f in @($OutLog, $ShotPpm)) { if (Test-Path $f) { Remove-Item $f -Force } }
$qemu = "C:\Program Files\qemu\qemu-system-x86_64.exe"
$biosDir = Split-Path $BiosImg; $biosLeaf = Split-Path $BiosImg -Leaf
$outLeaf = Split-Path $OutLog -Leaf
$OutLog = Join-Path $biosDir $outLeaf
if (Test-Path $OutLog) { Remove-Item $OutLog -Force }
$qargs = @(
    "-drive", "format=raw,file=$biosLeaf",
    "-display", "none", "-m", "1G", "-no-reboot", "-nographic",
    "-serial", "file:$outLeaf",
    "-qmp", "tcp:127.0.0.1:$QmpPort,server,nowait"
)
$qproc = Start-Process -FilePath $qemu -ArgumentList ($qargs -join " ") -WorkingDirectory $biosDir -PassThru -NoNewWindow
$sw = [System.Diagnostics.Stopwatch]::StartNew()
$CAP = '{"execute":"qmp_capabilities"}'
function Qmp-Send($json) {
    try {
        $c = New-Object System.Net.Sockets.TcpClient
        $c.Connect("127.0.0.1", $QmpPort)
        $s = $c.GetStream()
        $buf = New-Object byte[] 16384
        Start-Sleep -Milliseconds 80
        $null = $s.Read($buf, 0, 16384)
        $b = [System.Text.Encoding]::ASCII.GetBytes($CAP + [char]10)
        $s.Write($b, 0, $b.Length); $s.Flush()
        Start-Sleep -Milliseconds 80
        $null = $s.Read($buf, 0, 16384)
        $b2 = [System.Text.Encoding]::ASCII.GetBytes($json + [char]10)
        $s.Write($b2, 0, $b2.Length); $s.Flush()
        Start-Sleep -Milliseconds 150
        $c.Close()
    } catch { Write-Host "WARN qmp: $_" }
}
function Type-String($s) {
    $map = @{ " " = "spc"; "." = "dot"; "/" = "slash"; "-" = "minus"; "_" = "shift-minus"; "(" = "shift-9"; ")" = "shift-0"; "=" = "equal" }
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
        $b = [System.Text.Encoding]::ASCII.GetBytes($CAP + [char]10)
        $st.Write($b, 0, $b.Length); $st.Flush()
        Start-Sleep -Milliseconds 80
        $null = $st.Read($buf, 0, 16384)
        foreach ($k in $keys) {
            $j = '{"execute":"human-monitor-command","arguments":{"command-line":"sendkey ' + $k + '"}}'
            $b2 = [System.Text.Encoding]::ASCII.GetBytes($j + [char]10)
            $st.Write($b2, 0, $b2.Length); $st.Flush()
            Start-Sleep -Milliseconds 110
        }
        $j = '{"execute":"human-monitor-command","arguments":{"command-line":"sendkey ret"}}'
        $b2 = [System.Text.Encoding]::ASCII.GetBytes($j + [char]10)
        $st.Write($b2, 0, $b2.Length); $st.Flush()
        Start-Sleep -Milliseconds 110
        $c.Close()
    } catch { Write-Host "WARN typing: $_" }
}
function Read-Log { if (Test-Path $OutLog) { Get-Content $OutLog -Raw } else { "" } }
function Shot([string]$path) {
    $fwd = $path.Replace('\', "/")
    $j = '{"execute":"screendump","arguments":{"filename":"' + $fwd + '"}}'
    Qmp-Send $j
    Start-Sleep -Milliseconds 800
}

$shell = $false
while ($sw.ElapsedMilliseconds -lt 300000) {
    if ((Read-Log) -match "m8: shell ready") { $shell = $true; break }
    Start-Sleep -Seconds 3
}
Write-Host ("shell: " + $shell + " at " + [int]$sw.Elapsed.TotalSeconds + "s")
if (-not $shell) { Get-Content $OutLog -Tail 12 -ErrorAction SilentlyContinue; Stop-Process -Id $qproc.Id -Force; exit 1 }

# launch the H.264 player
Type-String "media /home/testvideo-h264.mp4"
$ready = $false
for ($i = 0; $i -lt 120; $i++) {
    Start-Sleep -Seconds 2
    if ((Read-Log) -match "mplayer: MP4 screen ready") { $ready = $true; break }
}
Write-Host ("player ready: " + $ready + " (first decode may be slow under TCG)")
Start-Sleep -Seconds 4
Shot $ShotPpm
Write-Host ("shot -> " + $ShotPpm)

# pixel check: decoded testsrc2 frame = saturated primary colors
$colorful = 0
if (Test-Path $ShotPpm) {
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
    for ($y = 90; $y -lt [Math]::Min($h - 60, 320); $y += 3) {
        for ($x = 100; $x -lt [Math]::Min($w - 100, 1820); $x += 5) {
            $o = $idx + ($y * $w + $x) * 3
            if ($o + 2 -lt $b.Length) {
                $r = $b[$o]; $g = $b[$o + 1]; $bl = $b[$o + 2]
                $mx = [Math]::Max($r, [Math]::Max($g, $bl))
                $mn = [Math]::Min($r, [Math]::Min($g, $bl))
                if (($mx - $mn) -gt 60 -and $mx -gt 60) { $colorful++ }
            }
        }
    }
}
Write-Host ("colorful video pixels: " + $colorful)
Write-Host ("PASS: " + ($ready -and $colorful -gt 40))
Qmp-Send '{"execute":"human-monitor-command","arguments":{"command-line":"sendkey esc"}}'
Start-Sleep -Seconds 2
Type-String "exit"
Start-Sleep -Seconds 3
Stop-Process -Id $qproc.Id -Force -ErrorAction SilentlyContinue
if ($ready -and $colorful -gt 40) { exit 0 } else { exit 1 }
