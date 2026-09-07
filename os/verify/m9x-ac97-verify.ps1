# m9x-ac97-verify.ps1 -- M9.7 boot gate: REAL PCM audio via the AC97 device.
# Boots bios.img with -device AC97 + a WAV backend, launches the media
# player on /home/testaudio.MP3, and proves the captured WAV is real
# decoded music (many distinct 16-bit sample values) rather than the
# old square-wave tone rendition.
param(
    [string]$BiosImg = "target\bios.img"
    ,[int]$QmpPort = 4598
    ,[string]$OutLog = "target\m9x-ac97-serial.log"
    ,[string]$WavPath = "target\m9x-ac97-audio.wav"
    ,[string]$ShotPpm = "target\m9x-ac97.ppm"
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
if (-not [System.IO.Path]::IsPathRooted($BiosImg)) { $BiosImg = Join-Path (Get-Location) $BiosImg }
if (-not [System.IO.Path]::IsPathRooted($OutLog)) { $OutLog = Join-Path (Get-Location) $OutLog }
if (-not [System.IO.Path]::IsPathRooted($WavPath)) { $WavPath = Join-Path (Get-Location) $WavPath }
if (-not [System.IO.Path]::IsPathRooted($ShotPpm)) { $ShotPpm = Join-Path (Get-Location) $ShotPpm }
Get-Process qemu-system-x86_64 -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
Start-Sleep -Milliseconds 500
if (-not (Test-Path $BiosImg)) { Write-Error "no $BiosImg"; exit 2 }
foreach ($f in @($OutLog, $WavPath, $ShotPpm)) { if (Test-Path $f) { Remove-Item $f -Force } }

$qemu = "C:\Program Files\qemu\qemu-system-x86_64.exe"
Write-Host "M9.7: booting $BiosImg (ac97 + wav -> $WavPath)"
$biosDir = Split-Path $BiosImg; $biosLeaf = Split-Path $BiosImg -Leaf
$outLeaf = Split-Path $OutLog -Leaf
$OutLog = Join-Path $biosDir $outLeaf
if (Test-Path $OutLog) { Remove-Item $OutLog -Force }
$wavLeaf = Split-Path $WavPath -Leaf
$WavPath = Join-Path $biosDir $wavLeaf
if (Test-Path $WavPath) { Remove-Item $WavPath -Force }
$qemuErr = Join-Path (Get-Location) "m9x-ac97-qemu-stderr.log"
if (Test-Path $qemuErr) { Remove-Item $qemuErr -Force }
$qargs = @(
    "-drive", "format=raw,file=$biosLeaf",
    "-display", "none",
    "-m", "1G",
    "-no-reboot",
    "-nographic",
    "-serial", "file:$outLeaf",
    "-qmp", "tcp:127.0.0.1:$QmpPort,server,nowait",
    "-audiodev", "wav,id=w0,path=$wavLeaf",
    "-device", "AC97,audiodev=w0",
    "-nic", "none"
)
try {
    $qproc = Start-Process -FilePath $qemu -ArgumentList ($qargs -join ' ') -WorkingDirectory $biosDir -PassThru -NoNewWindow -RedirectStandardError $qemuErr
} catch {
    Write-Host "M9.7: Start-Process failed: $($_.Exception.Message)"
    exit 1
}
$sw = [System.Diagnostics.Stopwatch]::StartNew()
Write-Host "M9.7: waiting for boot (up to 900s)"
Start-Sleep -Seconds 2
if ($qproc.HasExited) {
    Write-Host "M9.7: FAIL - QEMU exited immediately (code '$($qproc.ExitCode)')"
    Get-Content $qemuErr -ErrorAction SilentlyContinue | ForEach-Object { Write-Host "  qemu: $_" }
    exit 1
}

function Qmp-Send($json) {
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
        Start-Sleep -Milliseconds 150
        $c.Close()
    } catch { Write-Host "M9.7: WARN qmp $json failed: $_" }
}

function Send-Human($cmdline) {
    Qmp-Send ('{"execute":"human-monitor-command","arguments":{"command-line":"' + $cmdline + '"}}')
}

function Shot-Qmp([string]$path) {
    $fwd = $path.Replace("\", "/")
    $j = '{"execute":"screendump","arguments":{"filename":"' + $fwd + '"}}'
    Qmp-Send $j
    Start-Sleep -Milliseconds 800
    Write-Host "M9.7: shot -> $path"
}

function Type-String($s) {
    $map = @{ " " = "spc"; "." = "dot"; "/" = "slash"; "-" = "minus"; "," = "comma"
              "'" = "apostrophe"; "(" = "shift-9"; ")" = "shift-0"; "_" = "shift-minus"; "=" = "equal" }
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
            Start-Sleep -Milliseconds 130
        }
        # submit the line (the shell only evaluates on Enter; QEMU's X11
        # keysym for Return is 'ret')
        $j = '{"execute":"human-monitor-command","arguments":{"command-line":"sendkey ret"}}'
        $b2 = [System.Text.Encoding]::ASCII.GetBytes($j + [char]10)
        $st.Write($b2, 0, $b2.Length); $st.Flush()
        Start-Sleep -Milliseconds 130
        $c.Close()
    } catch { Write-Host "M9.7: WARN typing $s failed: $_" }
}

function Read-Log {
    if (Test-Path $OutLog) { Get-Content $OutLog -Raw } else { "" }
}

$passed = 0
function Check($name, $ok, $extra) {
    if ($ok) { $script:passed++; Write-Host "M9.7 PASS $name $extra" }
    else { Write-Host "M9.7 FAIL $name $extra" }
}

# ---- wait for the shell -----------------------------------------------
$shell = $false
$phaseInterp = 0
$phaseShell = 0
while ($sw.Elapsed.TotalSeconds -lt 900) {
    $total = Read-Log
    if ($phaseInterp -eq 0 -and $total -match "Interpreter ready") {
        $phaseInterp = [int]$sw.Elapsed.TotalSeconds
        Write-Host "M9.7: interpreter ready at ${phaseInterp}s"
    }
    if ($phaseShell -eq 0 -and $total -match "m8: shell ready|m9: shell ready") {
        $phaseShell = [int]$sw.Elapsed.TotalSeconds
        Write-Host "M9.7: shell ready at ${phaseShell}s"
    }
    if ($total -match "m9: shell ready|m8: shell ready") { $shell = $true; break }
    if ($qproc.HasExited) { break }
    Start-Sleep -Seconds 5
}
Check "boots to shell" $shell "($([int]$sw.Elapsed.TotalSeconds)s)"
if (-not $shell) { Write-Host '--- serial tail ---'; Get-Content $OutLog -Tail 30 -ErrorAction SilentlyContinue; Stop-Process -Id $qproc.Id -Force -ErrorAction SilentlyContinue; exit 1 }
$total = Read-Log
Check "AC97 driver initialized" ($total -match "audio: AC97 ready") ""

# ---- extract fixtures + launch the media player ----------------------
# testaudio.MP3 is embedded directly in /home (the long extract line
# overflowed the keyboard ring); just launch the player.
Type-String "media /home/testaudio.MP3"
$mediaSeen = $false
$audioSeen = $false
for ($i = 0; $i -lt 40; $i++) {
    Start-Sleep -Seconds 4
    $total = Read-Log
    if (-not $mediaSeen -and $total -match "mplayer: MP3 screen ready") {
        $mediaSeen = $true
        Write-Host "M9.7: player screen ready"
        Start-Sleep -Seconds 3
        Shot-Qmp $ShotPpm
    }
    if (-not $audioSeen -and $total -match "mplayer: audio ready|no tone track") {
        $audioSeen = $true
        Write-Host "M9.7: audio path engaged"
    }
    if ($mediaSeen -and $audioSeen) { break }
}
Check "media player loads MP3" $mediaSeen ""
# let real audio stream into the WAV for a while
Start-Sleep -Seconds 20

# ---- WAV analysis: real decoded music, not a square wave -------------
# Quit the MP3 player first so the WAV stops growing (it plays on loop).
Send-Human "sendkey esc"
Start-Sleep -Seconds 3
$wav = $null
# QEMU's wav backend keeps the file open for the whole VM run: read with
# FileShare.ReadWrite, and decode samples with BitConverter (the raw
# [int16] cast overflows on values > 32767).  The analysis is BOUNDED to
# the first MAX_SAMPLES: a full capture is tens of MB / millions of
# samples, and PowerShell's Sort-Object -Unique cannot handle that.  A
# window is plenty to prove real PCM.
$MAX_SAMPLES = 200000
if ((Test-Path $WavPath) -and (Get-Item $WavPath).Length -gt 100) {
    $fs = [System.IO.File]::Open($WavPath, 'Open', 'Read', 'ReadWrite')
    $bytes = New-Object byte[] $fs.Length
    $null = $fs.Read($bytes, 0, $bytes.Length)
    $fs.Close()
    if ($bytes.Length -gt 44) {
        $endIdx = [Math]::Min($bytes.Length, 44 + $MAX_SAMPLES * 2)
        $samples = New-Object 'System.Collections.Generic.List[int16]'
        for ($i = 44; $i + 1 -lt $endIdx; $i += 2) {
            $samples.Add([System.BitConverter]::ToInt16($bytes, $i))
        }
        $n = $samples.Count
        $sum = 0.0
        $peak = 0
        $distinctSet = New-Object 'System.Collections.Generic.HashSet[int16]'
        $loud = 0
        foreach ($s in $samples) {
            $a = [Math]::Abs([int]$s)
            $sum += $a
            if ($a -gt $peak) { $peak = $a }
            [void]$distinctSet.Add($s)
            if ($a -gt 400) { $loud++ }
        }
        $avg = if ($n -gt 0) { $sum / $n } else { 0.0 }
        $distinct = $distinctSet.Count
        Write-Host ("M9.7: wav bytes=$($bytes.Length) window=$n avg={0:N1} peak=$peak distinct=$distinct loud=$loud" -f $avg)
        Check "WAV captured audio" ($n -gt 1000 -and $avg -gt 50) "(n=$n avg=$avg)"
        Check "WAV is real PCM (many distinct values)" ($distinct -gt 100) "(distinct=$distinct)"
        Check "WAV has loud content" ($loud -gt ($n / 20)) "(loud=$loud of $n)"
    } else {
        Write-Host "M9.7: WAV too small: $($bytes.Length)"
    }
} else {
    Write-Host "M9.7: no WAV captured at $WavPath"
    Check "WAV captured audio" $false ""
    Check "WAV is real PCM" $false ""
    Check "WAV has loud content" $false ""
}


# ---- GIF video: the player must actually SHOW moving frames -----------
# (the MP3 player was already quit before the WAV analysis above)
Type-String "media /home/testanim.gif"
$gifSeen = $false
for ($i = 0; $i -lt 30; $i++) {
    Start-Sleep -Seconds 4
    if ((Read-Log) -match "mplayer: GIF screen ready") { $gifSeen = $true; break }
}
Check "media player loads animated GIF" $gifSeen ""
if ($gifSeen) {
    Start-Sleep -Seconds 3
    $shot1 = Join-Path (Split-Path $ShotPpm) "m9x-gif1.ppm"
    $shot2 = Join-Path (Split-Path $ShotPpm) "m9x-gif2.ppm"
    Shot-Qmp $shot1
    # 1.5 s is not a multiple of the 1.2 s GIF loop, so the two shots
    # must show different frames.
    Start-Sleep -Milliseconds 1500
    Shot-Qmp $shot2
    function Find-Ball([string]$path) {
        if (-not (Test-Path $path)) { return $null }
        $b = [System.IO.File]::ReadAllBytes($path)
        # P6 header: P6\n<w> <h>\n255\n
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
        $found = 0
        $sx = 0.0; $sy = 0.0
        $step = 3
        for ($y = 0; $y -lt $h; $y += $step) {
            for ($x = 0; $x -lt $w; $x += $step) {
                $o = $idx + ($y * $w + $x) * 3
                if ($o + 2 -lt $b.Length) {
                    $r = $b[$o]; $g = $b[$o + 1]; $bb = $b[$o + 2]
                    if ($r -gt 220 -and $g -gt 50 -and $g -lt 210 -and $bb -gt 40 -and $bb -lt 90) {
                        $found++
                        $sx += $x; $sy += $y
                    }
                }
            }
        }
        if ($found -eq 0) { return $null }
        return @{ count = $found; cx = [int]($sx / $found); cy = [int]($sy / $found) }
    }
    $b1 = Find-Ball $shot1
    $b2 = Find-Ball $shot2
    Check "GIF video frame rendered (ball pixels)" ($null -ne $b1) "(count=$($b1.count))"
    if ($null -ne $b1 -and $null -ne $b2) {
        $moved = ($b1.cx -ne $b2.cx) -or ($b1.cy -ne $b2.cy)
        Check "GIF video is animating (ball moved)" $moved "(@($($b1.cx),$($b1.cy)) -> @($($b2.cx),$($b2.cy)))"
    } else {
        Check "GIF video is animating (ball moved)" $false "(second shot empty)"
    }
} else {
    Check "GIF video frame rendered (ball pixels)" $false ""
    Check "GIF video is animating (ball moved)" $false ""
}

# ---- quit cleanly -----------------------------------------------------
Type-String "exit"
Start-Sleep -Seconds 5
if (-not $qproc.HasExited) { Stop-Process -Id $qproc.Id -Force -ErrorAction SilentlyContinue }
Write-Host "M9.7: $passed checks passed"
if ($passed -lt 9) { exit 1 }
exit 0
