# vbox-setup.ps1 -- run the Interpretive OS in Oracle VirtualBox.
#
# 1. Converts the freshly built BIOS image to a VDI
# 2. Creates a headless-friendly VM (BIOS firmware, PS/2 keyboard, VBE graphics)
# 3. Attaches a raw-file serial port so the debug console is captured
# 4. Starts the VM
#
# Usage:  powershell -File vbox-setup.ps1 [-VmName InterpretiveOS] [-VBox C:\Program Files\Oracle\VirtualBox\VBoxManage.exe]

param(
    [string]$VmName = "InterpretiveOS"
    ,[string]$VBox = "E:\VirtualBox\VBoxManage.exe"
    ,[switch]$Headless
)

$ErrorActionPreference = "Stop"
$root = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path   # os/

function VBox-Exec {
    # NB: the parameter must NOT be named $Args -- that collides with
    # PowerShell's automatic variable and makes the splat pass nothing.
    param([string[]]$CmdArgs)
    # VBox 7.x emits deprecation WARNINGS on stderr; with
    # $ErrorActionPreference=Stop, PS 5.1 would treat them as errors, so
    # merge stderr and only fail on a real non-zero exit code.
    $out = & $VBox @CmdArgs 2>&1
    if ($LASTEXITCODE -ne 0) {
        Write-Host ($out | Out-String)
        Write-Error ("VBoxManage failed: " + ($CmdArgs -join ' '))
        exit 1
    }
    $out
}

# 1. locate the freshly built image (canonical copy made by os/build.rs;
#    the OUT_DIR hash directory version is NOT used -- stale ones accumulate
#    there and mtimes lie, which previously booted a stale kernel)
$img = Join-Path $root 'target\bios.img'
if (-not (Test-Path $img)) { Write-Error 'no target/bios.img found; run: cargo build (in os/)'; exit 2 }
Write-Host "bios.img: $img"

# 2. convert raw image to VDI (only if the VDI is missing or older)
$vdi = Join-Path $root "target\$VmName.vdi"
if (-not (Test-Path $vdi) -or (Get-Item $vdi).LastWriteTime -lt (Get-Item $img).LastWriteTime) {
    # convertfromraw refuses to overwrite; drop the stale copy first.  The
    # VM must be stopped for this to succeed.
    if (Test-Path $vdi) {
        Write-Host "removing stale VDI: $vdi"
        Remove-Item $vdi -Force
    }
    Write-Host 'converting bios.img -> VDI ...'
    VBox-Exec @('convertfromraw', "$img", "$vdi", '--format', 'VDI')
} else { Write-Host "VDI up to date: $vdi" }

# 3. create/configure the VM
$vmPat = '"' + $VmName + '"'
$exists = & $VBox list vms 2>$null | Select-String -SimpleMatch $vmPat
if (-not $exists) {
    Write-Host 'creating VM ...'
    VBox-Exec @('createvm', '--name', $VmName, '--ostype', 'Linux26_64', '--register')
}

VBox-Exec @('modifyvm', $VmName, '--memory', '1024', '--vram', '64', '--firmware', 'bios', '--keyboard', 'ps2', '--graphicscontroller', 'vmsvga', '--nic1', 'none', '--audio-driver', 'none', '--usb', 'off', '--boot1', 'disk', '--ioapic', 'off')

# serial COM1 (0x3F8, IRQ4) -> raw file for the debug console
$serialLog = Join-Path $root 'target\vbox-serial.log'
if (Test-Path $serialLog) { Remove-Item $serialLog -Force }
VBox-Exec @('modifyvm', $VmName, '--uart1', '0x3F8', '4', '--uart-type1', '16550A', '--uart-mode1', 'file', "$serialLog")

# 4. attach the disk
# NB: VBox 7.x showvminfo prints "#0: 'SATA', Type: IntelAhci, ..." and
# "Location: \"...vdi\"" -- match those, not the 6.x 'SATA Controller' text.
$ctl = 'SATA'
$hasCtl = & $VBox showvminfo $VmName 2>$null | Select-String ("'" + $ctl + "'")
if (-not $hasCtl) { VBox-Exec @('storagectl', $VmName, '--name', $ctl, '--add', 'sata', '--controller', 'IntelAhci', '--portcount', '1') }
$vdiName = [System.IO.Path]::GetFileName($vdi)
$hasDisk = & $VBox showvminfo $VmName 2>$null | Select-String $vdiName
# Always re-attach: re-conversion gives the VDI a new UUID, but the media
# registry keeps the old one (State: inaccessible), which makes startvm fail
# with a UUID mismatch.  Detach, close the stale registry medium (so attach
# re-reads the file's real UUID), then attach.
if ($hasDisk) {
    VBox-Exec @('storageattach', $VmName, '--storagectl', $ctl, '--port', '0', '--device', '0', '--type', 'hdd', '--medium', 'none')
}
& $VBox closemedium disk "$vdi" 2>$null | Out-Null   # ignore: already gone is fine
VBox-Exec @('storageattach', $VmName, '--storagectl', $ctl, '--port', '0', '--device', '0', '--type', 'hdd', '--medium', "$vdi")

Write-Host "serial log: $serialLog"
Write-Host 'starting VM ...'
if ($Headless) {
    & $VBox startvm $VmName --type headless
} else {
    & $VBox startvm $VmName   # windowed: click into the VM to capture the keyboard
}

Write-Host ''
Write-Host 'The OS boots to the M8 interactive Python REPL shell.  Click into the VM'
Write-Host 'window (keyboard is captured when the VM has focus) and type Python at the'
Write-Host '`>>>` prompt, e.g.:  1 + 1  |  import kern  |  kern.tick()  |  help'
Write-Host ''
Write-Host 'Note: `exit` writes to QEMU-only isa-debug-exit, so it will NOT power off'
Write-Host 'the VirtualBox VM (it just hangs).  Close the VM window / `VBoxManage'
Write-Host 'controlvm <name> poweroff` to stop it.  The framebuffer is 1280x1024 here.'
Write-Host ''
Write-Host 'Serial debug console: tail -f target/vbox-serial.log  (or Get-Content -Wait)'