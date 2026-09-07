# vbox-drive.ps1 -- drive the Ripos VM: editor pane, F5, language, resolution.
param(
    [string]$VmName = "Ripos",
    [string]$VBox = "E:\VirtualBox\VBoxManage.exe",
    [string]$SerialLog = "target\verify-serial.log",
    [string]$OutDir = "target\vbox-repro"
)
$ErrorActionPreference = "Stop"
New-Item -ItemType Directory -Force -Path $OutDir | Out-Null
try { if (Test-Path $SerialLog) { Remove-Item $SerialLog -Force -ErrorAction SilentlyContinue } } catch { }

$map = @{
    'a'=0x1E;'b'=0x30;'c'=0x2E;'d'=0x20;'e'=0x12;'f'=0x21;'g'=0x22;'h'=0x23;'i'=0x17;'j'=0x24
    'k'=0x25;'l'=0x26;'m'=0x32;'n'=0x31;'o'=0x18;'p'=0x19;'q'=0x10;'r'=0x13;'s'=0x1F;'t'=0x14
    'u'=0x16;'v'=0x2F;'w'=0x11;'x'=0x2D;'y'=0x15;'z'=0x2C
    '1'=0x02;'2'=0x03;'3'=0x04;'4'=0x05;'5'=0x06;'6'=0x07;'7'=0x08;'8'=0x09;'9'=0x0A;'0'=0x0B
    " " = 0x39; "/" = 0x35; "." = 0x34; "-" = 0x0C; "=" = 0x0D; "[" = 0x1A; "]" = 0x1B; "," = 0x33; ";" = 0x27; "\" = 0x2B; "'" = 0x28
}
$shifted = @{ "(" = 0x0A; ")" = 0x0B; ":" = 0x27; "_" = 0x0C; "*" = 0x37; "+" = 0x0D }
function Send-Scan([string[]]$codes) {
    $args = @("controlvm", $VmName, "keyboardputscancode") + $codes
    & $VBox @args *> $null
    Start-Sleep -Milliseconds 120
}
function Type-Text([string]$Text) {
    $seq = @()
    foreach ($ch in $Text.ToCharArray()) {
        if ($shifted.ContainsKey([string]$ch)) {
            $sc = $shifted[[string]$ch]
            $seq += "2A"; $seq += ("{0:X2}" -f $sc); $seq += "AA"; $seq += ("{0:X2}" -f ($sc + 0x80))
        } elseif ($map.ContainsKey([string]$ch)) {
            $sc = $map[[string]$ch]
            $seq += ("{0:X2}" -f $sc); $seq += ("{0:X2}" -f ($sc + 0x80))
        }
        if ($seq.Count -ge 16) { Send-Scan $seq; $seq = @() }
    }
    if ($seq.Count -gt 0) { Send-Scan $seq }
    Write-Host "typed: $Text"
}
function Key([string]$name) {
    $sc = switch ($name) {
        "enter" { 0x1C } "esc" { 0x01 } "f5" { 0x3F } "f6" { 0x40 } "back" { 0x0E }
        "tab" { 0x0F } "up" { 0x48 } "down" { 0x50 } "space" { 0x39 }
    }
    Send-Scan @(("{0:X2}" -f $sc), ("{0:X2}" -f ($sc + 0x80)))
    Write-Host "key: $name"
}
function Shot([string]$name) {
    & $VBox controlvm $VmName screenshotpng (Join-Path $OutDir $name) *> $null
    Start-Sleep -Milliseconds 400
    Write-Host "shot: $name"
}

Write-Host "waiting for boot..."
$booted = $false
$sw = [System.Diagnostics.Stopwatch]::StartNew()
while ($sw.ElapsedMilliseconds -lt 120000) {
    if (Test-Path $SerialLog) {
        $t = Get-Content $SerialLog -Raw -ErrorAction SilentlyContinue
        if ($t -match "m8: shell ready") { $booted = $true; break }
    }
    Start-Sleep -Milliseconds 500
}
Write-Host ("booted=" + $booted + " at " + $sw.ElapsedMilliseconds + "ms")
if (-not $booted) { exit 2 }
Start-Sleep -Milliseconds 1500

# phase 1: open the editor
Type-Text "edit"; Key "enter"
$sw2 = [System.Diagnostics.Stopwatch]::StartNew()
$ed = $false
while ($sw2.ElapsedMilliseconds -lt 60000) {
    if ((Get-Content $SerialLog -Raw -ErrorAction SilentlyContinue) -match "tk: mainloop started") { $ed = $true; break }
    Start-Sleep -Milliseconds 400
}
Write-Host ("editor=" + $ed)
Start-Sleep -Milliseconds 1500
Shot "ed1-fresh.png"

# phase 2: F6 to pane, type print(1+1) and 1+1
Key "f6"; Start-Sleep -Milliseconds 600
Type-Text "print(1+1)"; Key "enter"
Start-Sleep -Milliseconds 3000
Shot "ed2-print.png"
Type-Text "1+1"; Key "enter"
Start-Sleep -Milliseconds 2000
Shot "ed3-expr.png"

# phase 3: F6 to editor, buffer + F5
Key "f6"; Start-Sleep -Milliseconds 600
Type-Text "x = 21"; Key "enter"
Type-Text "print(x)"; Start-Sleep -Milliseconds 500
Key "f5"
Start-Sleep -Milliseconds 4000
Shot "ed4-f5.png"

# phase 4: back to shell, set zh, open fm
Key "esc"; Start-Sleep -Milliseconds 2000
Type-Text "import settings; settings.set(" + [char]39 + "language" + [char]39 + "," + [char]39 + "zh" + [char]39 + ")"; Key "enter"
Start-Sleep -Milliseconds 1500
Type-Text "fm"; Key "enter"
Start-Sleep -Milliseconds 4000
Shot "fm-zh.png"

# phase 5: fm esc, open editor in zh
Key "esc"; Start-Sleep -Milliseconds 2000
Type-Text "edit"; Key "enter"
Start-Sleep -Milliseconds 4000
Shot "ed-zh.png"

# phase 6: back to shell, res 1920x1080
Key "esc"; Start-Sleep -Milliseconds 2000
Type-Text "res 1920x1080"; Key "enter"
Start-Sleep -Milliseconds 3000
Shot "res-1080.png"

Type-Text "res 1600x1200"; Key "enter"
Start-Sleep -Milliseconds 3000
Shot "res-1600.png"

Write-Host "=== serial tail ==="
Get-Content $SerialLog -Raw -ErrorAction SilentlyContinue | Select-Object -Last 1 | Out-String
Write-Host "DONE"