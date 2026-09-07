# m9x-media-verify.ps1 -- M9.6 boot gate: media player in QEMU.
# Boots the freshly built bios.img, waits for the shell, launches the
# media player on /home/testaudio.MP3, screendumps the framebuffer and
# pixel-checks the waveform/playhead/panel, then quits cleanly.
#
# Run:  powershell -File target/m9x-media-verify.ps1
param(
    [string]$BiosImg = "target\bios.img"
    ,[int]$QmpPort = 4597
    ,[string]$OutLog = "target\m9x-media-serial.log"
    ,[string]$ShotPpm = "target\m9x-media.ppm"
    ,[string]$ShotVideoPpm = "target\m9x-video.ppm"
)

$ErrorActionPreference = "Stop"
# QEMU's working directory is NOT our cwd, so absolutize every path it
# may open (drive file, serial output).
if (-not [System.IO.Path]::IsPathRooted($BiosImg)) { $BiosImg = Join-Path (Get-Location) $BiosImg }
if (-not [System.IO.Path]::IsPathRooted($OutLog)) { $OutLog = Join-Path (Get-Location) $OutLog }
if (-not [System.IO.Path]::IsPathRooted($ShotPpm)) { $ShotPpm = Join-Path (Get-Location) $ShotPpm }
if (-not [System.IO.Path]::IsPathRooted($ShotVideoPpm)) { $ShotVideoPpm = Join-Path (Get-Location) $ShotVideoPpm }
# A previous run's QEMU can outlive the harness (Start-Process children
# survive tool-level process kills) and hold the disk/serial handles.
Get-Process qemu-system-x86_64 -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
Start-Sleep -Milliseconds 500
if (-not (Test-Path $BiosImg)) { Write-Error "no $BiosImg"; exit 2 }
if (Test-Path $OutLog) { Remove-Item $OutLog -Force }
if (Test-Path $ShotPpm) { Remove-Item $ShotPpm -Force }
if (Test-Path $ShotVideoPpm) { Remove-Item $ShotVideoPpm -Force }

$qemu = "C:\Program Files\qemu\qemu-system-x86_64.exe"
Write-Host "M9.6: booting $BiosImg (serial -> $OutLog, qmp $QmpPort)"
# -nographic keeps the serial chardev serviced in this environment;
# -display none alone made QEMU drop all serial output.
$biosDir = Split-Path $BiosImg; $biosLeaf = Split-Path $BiosImg -Leaf
$outLeaf = Split-Path $OutLog -Leaf
# QEMU creates the serial file in its own working directory ($biosDir), so
# point $OutLog there for Read-Log.
$OutLog = Join-Path $biosDir $outLeaf
if (Test-Path $OutLog) { Remove-Item $OutLog -Force }
# Start-Process mangles arguments that contain spaces, so run QEMU from the
# image's directory and use space-free relative paths for drive/serial.
$qemuErr = Join-Path (Get-Location) "m9x-media-qemu-stderr.log"
if (Test-Path $qemuErr) { Remove-Item $qemuErr -Force }
$audioWav = Join-Path $biosDir "m9x-media-audio.wav"
if (Test-Path $audioWav) { Remove-Item $audioWav -Force }
$qargs = @(
    "-drive", "format=raw,file=$biosLeaf",
    "-display", "none",
    "-m", "1G",
    "-no-reboot",
    "-nographic",
    "-serial", "file:$outLeaf",
    "-qmp", "tcp:127.0.0.1:$QmpPort,server,nowait",
    "-audiodev", "wav,id=w0,path=m9x-media-audio.wav",
    "-machine", "pc,pcspk-audiodev=w0"
)
try {
    $qproc = Start-Process -FilePath $qemu -ArgumentList ($qargs -join ' ') -WorkingDirectory $biosDir -PassThru -NoNewWindow -RedirectStandardError $qemuErr
} catch {
    Write-Host "M9.6: Start-Process failed: $($_.Exception.Message)"
    exit 1
}
$sw = [System.Diagnostics.Stopwatch]::StartNew()
Write-Host "M9.6: waiting for boot (up to 900s)"
Start-Sleep -Seconds 2
if ($qproc.HasExited) {
    Write-Host "M9.6: FAIL - QEMU exited immediately (code '$($qproc.ExitCode)')"
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
    } catch { Write-Host "M9.6: WARN qmp $json failed: $_" }
}

function Send-Human($cmdline) {
    Qmp-Send ('{"execute":"human-monitor-command","arguments":{"command-line":"' + $cmdline + '"}}')
}

function Shot-Qmp([string]$path) {
    # QMP screendump accepts an absolute path (HMP screendump chokes on
    # spaces in the path); forward slashes keep QEMU happy on Windows.
    $fwd = $path.Replace("\", "/")
    $j = '{"execute":"screendump","arguments":{"filename":"' + $fwd + '"}}'
    Qmp-Send $j
    Start-Sleep -Milliseconds 800
    Write-Host "M9.6: shot -> $path"
}

function Type-String($s) {
    $map = @{ " " = "spc"; "." = "dot"; "/" = "slash"; "-" = "minus"
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
        $c.Close()
    } catch { Write-Host "M9.6: WARN typing $s failed: $_" }
}

$step = 0
$deadline = $sw.ElapsedMilliseconds + 2400000
$fail = $false
$mediaRetry = 0

function Read-Log {
    try {
        if (Test-Path $OutLog) {
            $fs = [System.IO.File]::Open($OutLog, [System.IO.FileMode]::Open, [System.IO.FileAccess]::Read, [System.IO.FileShare]::ReadWrite)
            try {
                $tr = New-Object System.IO.StreamReader($fs)
                $script:total = $tr.ReadToEnd()
            } finally { $fs.Close() }
        }
    } catch { }
}

function Set-Step($n) {
    $script:step = $n
    $script:stepT = $sw.ElapsedMilliseconds
}

while ($sw.ElapsedMilliseconds -lt $deadline) {
    if ($qproc.HasExited) {
        Write-Host "M9.6: FAIL - QEMU exited early (code $($qproc.ExitCode))"
        Get-Content $qemuErr -ErrorAction SilentlyContinue | ForEach-Object { Write-Host "  qemu: $_" }
        break
    }
    Read-Log
    if ($total -match "KERNEL PANIC|Fatal Python error") { $fail = $true; break }
    if ($step -eq 0 -and $total -match "m8: shell ready") {
        Write-Host "M9.6: shell up; extract testaudio from test.zip then launch media player"
        Start-Sleep -Milliseconds 600
        # /home now holds a single test.zip (the fixtures were consolidated).
        # Extract the MP3 in the shell first, then launch the media command.
        Type-String "import zipfile"
        Send-Human "sendkey ret"
        Start-Sleep -Milliseconds 300
        Type-String "zipfile.ZipFile('/home/test.zip').extractall('/home')"
        Send-Human "sendkey ret"
        Start-Sleep -Milliseconds 1200
        Type-String "media /home/testaudio.MP3"
        Send-Human "sendkey ret"
        Set-Step 1
    }
    elseif ($step -eq 1) {
        # Launching an app under a CPU-starved TCG can swallow the typed
        # command, so re-send it if 'mplayer: MP3' has not appeared.  Wait
        # for 'audio ready': the tone track is built lazily on first play
        # and the speaker starts emitting, so the captured WAV has sound.
        if ($total -match "mplayer: audio ready") {
            Write-Host "M9.6: playing; screendump"
            Start-Sleep -Milliseconds 2500
            Shot-Qmp $ShotPpm
            Start-Sleep -Milliseconds 800
            Send-Human "sendkey esc"
            Set-Step 2
        } elseif ($total -match "mplayer: MP3 screen ready") {
            # player drawn but tone track still building (slow TCG)
            if ($sw.ElapsedMilliseconds -gt ($script:stepT + 150000)) {
                Write-Host "M9.6: audio ready not seen; screendump anyway"
                Shot-Qmp $ShotPpm
                Start-Sleep -Milliseconds 800
                Send-Human "sendkey esc"
                Set-Step 2
            }
        } elseif ($sw.ElapsedMilliseconds -gt ($script:stepT + 120000) -and $mediaRetry -lt 4) {
            Write-Host "M9.6: media cmd not seen yet; retyping ($($mediaRetry + 1)/4)"
            Send-Human "sendkey esc"
            Start-Sleep -Milliseconds 400
            Type-String "import zipfile"
        Send-Human "sendkey ret"
        Start-Sleep -Milliseconds 300
        Type-String "zipfile.ZipFile('/home/test.zip').extractall('/home')"
            Send-Human "sendkey ret"
            Start-Sleep -Milliseconds 1200
            Type-String "media /home/testaudio.MP3"
            Send-Human "sendkey ret"
            $script:mediaRetry++
            Set-Step 1
        }
    }
    elseif ($step -eq 2 -and $total -match "mplayer: quit") {
        Write-Host "M9.6: MP3 player quit; launching MJPEG video"
        Start-Sleep -Milliseconds 400
        Type-String "media /home/testvideo-mjpeg.mp4"
        Send-Human "sendkey ret"
        Set-Step 3
    }
    elseif ($step -eq 3) {
        # The MJPEG video decodes its first frame on launch (mplayer: MP4
        # appears on load; the pure-Python JPEG decode takes a moment).
        if ($total -match "mplayer: MP4 screen ready") {
            Write-Host "M9.6: video frame decoded + drawn; screendump"
            Start-Sleep -Milliseconds 800
            Shot-Qmp $ShotVideoPpm
            Start-Sleep -Milliseconds 800
            Send-Human "sendkey esc"
            Set-Step 4
        } elseif ($total -match "mplayer: MP4") {
            # loaded but first frame still decoding (pure-Python JPEG)
            if ($sw.ElapsedMilliseconds -gt ($script:stepT + 120000)) {
                Write-Host "M9.6: video frame decode slow; screendump anyway"
                Shot-Qmp $ShotVideoPpm
                Start-Sleep -Milliseconds 800
                Send-Human "sendkey esc"
                Set-Step 4
            }
        } elseif ($sw.ElapsedMilliseconds -gt ($script:stepT + 120000) -and $mediaRetry -lt 4) {
            Write-Host "M9.6: video cmd not seen; retyping ($($mediaRetry + 1)/4)"
            Send-Human "sendkey esc"
            Start-Sleep -Milliseconds 400
            Type-String "media /home/testvideo-mjpeg.mp4"
            Send-Human "sendkey ret"
            $script:mediaRetry++
            Set-Step 3
        }
    }
    elseif ($step -eq 4 -and $total -match "mplayer: quit") {
        Write-Host "M9.6: video player quit; powering off"
        Start-Sleep -Milliseconds 300
        Type-String "exit"
        Send-Human "sendkey ret"
        Set-Step 5
    }
    elseif ($step -eq 5 -and $total -match "Ripos: powering off") { break }
    if ($step -gt 0 -and ($sw.ElapsedMilliseconds -gt ($script:stepT + 300000))) {
        Write-Host "M9.6: FAIL - step $step timed out (serial so far: '$($total.Substring([Math]::Max(0,$total.Length-200)))')"
        $fail = $true; break
    }
    Start-Sleep -Milliseconds 120
}

# Let QEMU exit on its own (isa-debug-exit) so the wav backend finalizes
# the header; only force-kill if it lingers.
$exitWait = 0
while (-not $qproc.HasExited -and $exitWait -lt 10000) {
    Start-Sleep -Milliseconds 200
    $exitWait += 200
}
if (-not $qproc.HasExited) { Stop-Process -Id $qproc.Id -Force -ErrorAction SilentlyContinue }

Write-Host ""
Write-Host "===== serial log tail ====="
($total -split "`n") | Select-Object -Last 25 | ForEach-Object { Write-Host $_ }

# ---- pixel check on the screendump ----
$pass = $true
function Check($name, [bool]$ok, $extra = "") {
    if ($ok) { Write-Host "M9.6 PASS: $name" -ForegroundColor Green }
    else { Write-Host "M9.6 FAIL: $name $extra" -ForegroundColor Red; $script:pass = $false }
}

if ((Test-Path $ShotPpm) -and (Get-Item $ShotPpm).Length -gt 1000) {
    $bytes = [System.IO.File]::ReadAllBytes($ShotPpm)
    $hdrEnd = -1
    for ($i = 0; $i -lt [Math]::Min(200, $bytes.Length); $i++) {
        if ($bytes[$i] -eq 10 -and $i -gt 0) {
            # find the pixel-data start: two lines of ascii after 'P6'
            $nl = 0
            for ($j = $i; $j -lt [Math]::Min(400, $bytes.Length); $j++) {
                if ($bytes[$j] -eq 10) { $nl++ }
                if ($nl -eq 3) { $hdrEnd = $j + 1; break }
            }
            break
        }
    }
    if ($hdrEnd -lt 0) { $hdrEnd = 200 }
    $dim = [System.Text.Encoding]::ASCII.GetString($bytes, 2, [Math]::Min(80, $hdrEnd - 2))
    $parts = ($dim -split "\s+") | Where-Object { $_ -ne "" }
    $w = [int]$parts[0]; $h = [int]$parts[1]
    $px = $hdrEnd
    function Get-Px($x, $y) {
        $o = $px + (($y * $w) + $x) * 3
        if ($o + 2 -ge $bytes.Length) { return @(0, 0, 0) }
        return @($bytes[$o], $bytes[$o + 1], $bytes[$o + 2])
    }
    Write-Host "M9.6: screendump ${w}x${h}, $($bytes.Length) bytes"
    $panelHits = 0
    $greenHits = 0
    for ($y = 40; $y -lt [Math]::Min($h - 20, 500); $y += 2) {
        for ($x = 0; $x -lt [Math]::Min($w, 512); $x += 2) {
            $c = Get-Px $x $y
            if ($c[0] -eq 22 -and $c[1] -eq 27 -and $c[2] -eq 38) { $panelHits++ }
            if (($c[0] -eq 146 -and $c[1] -eq 226 -and $c[2] -eq 150) -or ($c[0] -eq 56 -and $c[1] -eq 118 -and $c[2] -eq 72)) { $greenHits++ }
        }
    }
    # The playhead is a 1px white column that advances with playback; scan
    # a small x-window around the start of the waveform timeline instead of
    # a single stale coordinate.
    $phHits = 0
    for ($x = 125; $x -lt 175; $x++) {
        $c = Get-Px $x 110
        if ($c[0] -eq 255 -and $c[1] -eq 255 -and $c[2] -eq 255) { $phHits++ }
    }
    Check "panel drawn on screen" ($panelHits -gt 20) "($panelHits hits)"
    Check "waveform drawn on screen" ($greenHits -gt 10) "($greenHits hits)"
    Check "playhead visible" ($phHits -gt 0) "($phHits white px in window)"
} else {
    Check "screendump captured" $false "no ppm"
    $pass = $false
}

Check "media player loaded" ($total -match "mplayer: MP3")
Check "media player quit cleanly" ($total -match "mplayer: quit")
Check "clean poweroff" ($total -match "Ripos: powering off")
Check "no kernel panic" (-not ($total -match "KERNEL PANIC"))

# ---- M9.5: real sound out -- the PC speaker WAV must contain audio ----
$wavBytes = $null
if ((Test-Path $audioWav) -and (Get-Item $audioWav).Length -gt 100) {
    $wavBytes = [System.IO.File]::ReadAllBytes($audioWav)
    $nSamples = [Math]::Max(0, ($wavBytes.Length - 44) / 2)
    $energy = 0.0
    $nonzero = 0
    for ($i = 44; $i -lt [Math]::Min($wavBytes.Length, 44 + 80000); $i += 2) {
        $s = [BitConverter]::ToInt16($wavBytes, $i)
        $energy += [Math]::Abs($s)
        if ([Math]::Abs($s) -gt 40) { $nonzero++ }
    }
    $counted = [Math]::Max(1, (([Math]::Min($wavBytes.Length, 44 + 80000) - 44) / 2))
    $avg = $energy / $counted
    Write-Host "M9.6: audio wav $($wavBytes.Length) bytes, $nSamples samples, avg |s|=$($avg.ToString('F1')), loud=$nonzero"
    Check "audio wav captured" ($wavBytes.Length -gt 44)
    Check "speaker produced sound" ($avg -gt 30 -and $nonzero -gt 500) "(avg=$($avg.ToString('F1')) loud=$nonzero)"
} else {
    Check "audio wav captured" $false "no wav / too small"
}

# ---- M9.5: video frame display -- the MJPEG screendump must show the
# decoded frame (a colorful gradient), not just the grey chart ----
if ((Test-Path $ShotVideoPpm) -and (Get-Item $ShotVideoPpm).Length -gt 1000) {
    $vbytes = [System.IO.File]::ReadAllBytes($ShotVideoPpm)
    $vhdr = -1
    for ($i = 0; $i -lt [Math]::Min(200, $vbytes.Length); $i++) {
        if ($vbytes[$i] -eq 10 -and $i -gt 0) {
            $nl = 0
            for ($j = $i; $j -lt [Math]::Min(400, $vbytes.Length); $j++) {
                if ($vbytes[$j] -eq 10) { $nl++ }
                if ($nl -eq 3) { $vhdr = $j + 1; break }
            }
            break
        }
    }
    if ($vhdr -lt 0) { $vhdr = 200 }
    $vdim = [System.Text.Encoding]::ASCII.GetString($vbytes, 2, [Math]::Min(80, $vhdr - 2))
    $vparts = ($vdim -split "\s+") | Where-Object { $_ -ne "" }
    $vw = [int]$vparts[0]; $vh = [int]$vparts[1]
    $vpx = $vhdr
    function Get-VPx($x, $y) {
        $o = $vpx + (($y * $vw) + $x) * 3
        if ($o + 2 -ge $vbytes.Length) { return @(0, 0, 0) }
        return @($vbytes[$o], $vbytes[$o + 1], $vbytes[$o + 2])
    }
    Write-Host "M9.6: video screendump ${vw}x${vh}"
    # The video area spans the cavity width (sidebar+18 .. width-right).
    # The MJPEG frame is scaled to fit (max 180 tall, centered), so scan
    # the whole video band y=100..270 for clearly colored pixels.
    $colorHits = 0
    $sx0 = 100
    $sx1 = $vw - 100
    for ($y = 100; $y -lt 270 -and $y -lt $vh - 40; $y += 3) {
        for ($x = $sx0; $x -lt $sx1; $x += 5) {
            $c = Get-VPx $x $y
            $maxc = [Math]::Max($c[0], [Math]::Max($c[1], $c[2]))
            $minc = [Math]::Min($c[0], [Math]::Min($c[1], $c[2]))
            if (($maxc - $minc) -gt 60 -and $maxc -gt 60) { $colorHits++ }
        }
    }
    Check "video frame displayed" ($colorHits -gt 40) "($colorHits color hits)"
} else {
    Check "video screendump captured" $false "no video ppm"
}

if (-not $fail -and $pass) {
    Write-Host ""
    Write-Host "M9.6 GATE PASSED: media player boots and renders" -ForegroundColor Green
    exit 0
}
Write-Host ""
Write-Host "M9.6 GATE FAILED" -ForegroundColor Red
exit 1