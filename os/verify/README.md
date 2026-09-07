# os/verify 鈥?gate & test harnesses

The verification harnesses for Ripos live here (moved out of `os/target`
during the 2026-08-20 tidy; `target/` is now purely a cargo build directory).
The harnesses use **cwd-relative** `target\...` paths, so run them with the
working directory set to `os/` (i.e. `powershell -File verify\<name>.ps1`
from inside `os/`), after `cargo build` has regenerated `target/bios.img`.

## Host gates (no OS boot; Python 3 on the build machine)

Run from `os/` with `py -3`:

| Gate | Checks |
|---|---|
| `tk-test.py` | M8.5 tk toolkit regression (layout, focus, canvas, destroy, embedded window) |
| `tkinter-test.py` + `tkinter-conformance.py` | M8.9 tkinter compat incl. real-stdlib conformance |
| `mouse-test.py`, `fm-test.py`, `editor-test.py` | M9 mouse / file manager / editor |
| `jpeg-test.py`, `imgview-test.py`, `imgedit-test.py` | M9.2 images |
| `sevenz-test.py`, `rtf-docx-test.py`, `mp4-test.py` | M9.3 / M9.4 / M9.5 formats |
| `clipboard-test.py`, `mirror-toggle-test.py` | M9.6 clipboard/hotkeys, display mirror |
| `peinfo-test.py`, `x86-test.py`, `peexec-test.py`, `peexec-dll-test.py` | M10 PE parser / x86 interpreter / PE runner |
| `mplayer-test.py`, `arc-test.py`, `settings-test.py`, `switch-test.py`, `features-test.py`, `layout-test.py`, `png-test.py`, `boot-smoke-test.py`, `i18n-smoke.py` | apps & misc |
| `fonts/\*` | font assets (font8x8_basic.h, unifont.hex(+gz), make_modern_font.py, extract_font.py) |

## QEMU gates (boot the OS, inject input over serial-over-TCP + QMP)

| Harness | Milestone |
|---|---|
| `m6-verify2.ps1` | M6 framebuffer/keyboard drivers |
| `m7-verify.ps1` | M7 window manager + i18n |
| `m8-verify.ps1` | M8 boot-to-shell demo |
| `m9-verify.ps1` | M9 mouse / fm / editor / res |
| `m91-verify.ps1` | M9.1 zlib/bz2/lzma/elementtree |
| `m9x0-verify.ps1` | M9.x foundations |
| `m9x-media-verify.ps1` | M9.5 media player (audio WAV + video frames) |
| `m9x-ac97-verify.ps1` | M9.7 real PCM out via the AC'97 device (WAV distinct-value + GIF video checks) |
| `h264-verify.ps1` | H.264 baseline MP4 playback via the _h264 h264bsd module (pixel check) |
| `m9x-net-verify.ps1` | Phase 2 e1000 + smoltcp + browser (host HTTP fixture) |
| `vbox-setup.ps1`, `vbox-media.ps1`, `vbox-media-verify.ps1`, `vbox-drive.ps1` | VirtualBox 7.2 (converts `dist/bios.img` 鈫?VDI) |

Also here: `tk.py` (host-test copy of the toolkit), `pe_hello.exe` /
`pe_mydll.dll` (M10 test PEs), `m9x-media-audio.wav` (M9.5 gate evidence).
