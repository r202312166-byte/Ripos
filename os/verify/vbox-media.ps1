# vbox-media.ps1 -- drive the Ripos VM media player (sound + video).
param(
    [string]$VmName = "Ripos",
    [string]$VBox = "E:\VirtualBox\VBoxManage.exe",
    [string]$SerialLog = "target\vbox-home.log",
    [string]$OutDir = "target\vbox-media"
)
$ErrorActionPreference = "Stop"
New-Item -ItemType Directory -Force -Path $OutDir | Out-Null

$map = @{
    'a'=0x1E;'b'=0x30;'c'=0x2E;'d'=0x20;'e'=0x12;'f'=0x21;'g'=0x22;'h'=0x23;'i'=0x17;'j'=0x24
    'k'=0x25;'l'=0x26;'m'=0x32;'n'=0x31;'o'=0x18;'p'=0x19;'q'=0x10;'r'=0x13;'s'=0x1F;'t'=0x14
    'u'=0x16;'v'=0x2F;'w'=0x11;'x'=0x2D;'y'=0x15;'z'=0x2C
    '1'=0x02;'2'=0x03;'3'=0x04;'4'=0x05;'5'=0x06;'6'=0x07;'7'=0x08;'8'=0x09;'9'=0x0A;'0'=0x0B
    " "=0x39;"/"=0x35;"."=0x34;"-"=0x0C;"="=0x0D;"["=0x1A;"]"=0x1B;","=0x33;";"=0x27;"\"=0x2B;"'"=0x28;"+"=0x0D
}
$shifted = @{ "("=0x0A; ")"=0x0B; ":"=0x27; "_"=0x0C; "*"=0x37; "+"=0x0D }
function Send-Scan([string[]]$codes) {
    $args = @("controlvm", $VmName, "keyboardputscancode") + $codes
    & $VBox @args *> $null
    Start-Sleep -Milliseconds 100
}
function Type-Text([string]$Text) {
    $seq = @()
    foreach ($ch in $Text.ToCharArray()) {
        $lower = [string]$ch.ToString().ToLower()
        $isUpper = ($ch.ToString() -cne $lower)
        if ($shifted.ContainsKey([string]$ch)) {
            $sc = $shifted[[string]$ch]
            $seq += "2A"; $seq += ("{0:X2}" -f $sc); $seq += "AA"; $seq += ("{0:X2}" -f ($sc + 0x80))
        } elseif ($map.ContainsKey($lower)) {
            $sc = $map[$lower]
            if ($isUpper) { $seq += "2A" }
            $seq += ("{0:X2}" -f $sc); $seq += ("{0:X2}" -f ($sc + 0x80))
            if ($isUpper) { $seq += "AA" }
        }
        if ($seq.Count -ge 16) { Send-Scan $seq; $seq = @() }
    }
    if ($seq.Count -gt 0) { Send-Scan $seq }
    Write-Host "typed: $Text"
}
function Key([string]$name) {
    $sc = switch ($name) {
        "enter" { 0x1C } "esc" { 0x01 } "up" { 0x48 } "down" { 0x50 } "space" { 0x39 } "left" { 0x4B } "right" { 0x4D }
    }
    Send-Scan @(("{0:X2}" -f $sc), ("{0:X2}" -f ($sc + 0x80)))
    Write-Host "key: $name"
}
function Shot([string]$name) {
    & $VBox controlvm $VmName screenshotpng (Join-Path $OutDir $name) *> $null
    Start-Sleep -Milliseconds 500
    Write-Host "shot: $name"
}
function Read-Log {
    try { return (Get-Content $SerialLog -Raw -ErrorAction SilentlyContinue) } catch { return "" }
}

# extract the fixtures first (one line, then the extract)
Type-Text "import zipfile"; Key "enter"; Start-Sleep -Milliseconds 800
Type-Text "zipfile.ZipFile('/home/test.zip').extractall('/home')"; Key "enter"
Start-Sleep -Milliseconds 3000

# --- MP3: launch, wait for audio ready, screenshot, quit ---
Type-Text "media /home/testaudio.MP3"; Key "enter"
$sw = [System.Diagnostics.Stopwatch]::StartNew()
$audioReady = $false
while ($sw.ElapsedMilliseconds -lt 180000) {
    if ((Read-Log) -match "mplayer: audio ready") { $audioReady = $true; break }
    Start-Sleep -Milliseconds 500
}
Write-Host ("audio ready=" + $audioReady + " in " + [math]::Round($sw.ElapsedMilliseconds/1000,1) + "s")
Start-Sleep -Milliseconds 1500
Shot "mp3-playing.png"
Key "esc"; Start-Sleep -Milliseconds 1500

# --- MP4 MJPEG: launch, wait for MP4 screen ready, screenshot, quit ---
Type-Text "media /home/testvideo-mjpeg.mp4"; Key "enter"
$sw2 = [System.Diagnostics.Stopwatch]::StartNew()
$videoReady = $false
while ($sw2.ElapsedMilliseconds -lt 180000) {
    if ((Read-Log) -match "mplayer: MP4 screen ready") { $videoReady = $true; break }
    Start-Sleep -Milliseconds 500
}
Write-Host ("video ready=" + $videoReady + " in " + [math]::Round($sw2.ElapsedMilliseconds/1000,1) + "s")
Start-Sleep -Milliseconds 1500
Shot "mp4-video.png"
Key "esc"; Start-Sleep -Milliseconds 1500

Write-Host "=== serial tail ==="
Get-Content $SerialLog -Raw -ErrorAction SilentlyContinue | Select-Object -Last 1 | Out-String
Write-Host "DONE"