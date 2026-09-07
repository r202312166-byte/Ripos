# Builds and runs the host de-risk harness (MinGW GNU target).

$ErrorActionPreference = "Stop"

$pyRoot = "C:\Users\Administrator\AppData\Local\Python\pythoncore-3.14-64"
if (-not (Test-Path "$pyRoot\python314.dll")) {
    throw "Python 3.14 not found at $pyRoot -- edit this script."
}

$mingw = "C:\msys64\mingw64"
if (-not (Test-Path "$mingw\bin\gcc.exe")) {
    throw "MinGW-w64 not found at $mingw -- install MSYS2 mingw-w64-x86_64-gcc."
}

$env:PYTHONHOME = $pyRoot
$env:PYTHONPATH = "$pyRoot\Lib"
$env:PYTHON_LIBS = "$pyRoot\libs"
$env:PATH = "$mingw\bin;$pyRoot;$pyRoot\Scripts;$env:PATH"

$cargo = "$env:USERPROFILE\.cargo\bin\cargo.exe"
if (-not (Test-Path $cargo)) { throw "cargo not found at $cargo" }

Push-Location $PSScriptRoot
try {
    & $cargo run --quiet
} finally {
    Pop-Location
}