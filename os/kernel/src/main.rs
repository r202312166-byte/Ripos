#![no_std]
#![no_main]

extern crate alloc;

mod allocator;
mod audio;
mod framebuffer;
mod idt;
mod keyboard;
mod libc;
mod mouse;
mod net;
mod nic;
mod page;
mod pci;
mod pic;
mod pybind;
mod sched;
mod serial;
mod speaker;
mod time;
mod timer;
mod util;
mod vga;
mod vfs;

use bootloader_api::entry_point;
use core::panic::PanicInfo;

pub static BOOTLOADER_CONFIG: bootloader_api::BootloaderConfig = {
    let mut config = bootloader_api::BootloaderConfig::new_default();
    config.kernel_stack_size = 16 * 1024 * 1024; // 16 MiB
    // The embedded CPython is a true LP64 (SysV) build, so pointers are full
    // 64-bit; the stack no longer needs to be pinned below 4 GiB.  The fixed
    // address is kept as a stable, well-known location.
    config.mappings.kernel_stack = bootloader_api::config::Mapping::FixedAddress(0x3000_0000);
    // Map all physical memory into the virtual address space. The offset is
    // reported via `boot_info.physical_memory_offset`; the heaps live at
    // physical 0x4000000/0x6000000 and are accessed through that offset.
    config.mappings.physical_memory = Some(bootloader_api::config::Mapping::Dynamic);
    config
};

/// Physical addresses of the kernel heaps.  The bootloader identity-maps the
/// first 10 GiB, so physical == virtual here.  Both must stay inside the
/// usable RAM region and ABOVE the bootloader's own region (on VirtualBox
/// the bootloader marks [0x1000000, 0x4830000) as its own -- kernel image,
/// page tables, bump data).
///
/// The C heap is the memory CPython draws from (malloc/calloc).  It must be
/// large enough for the interpreter plus the full-screen UI double buffer
/// (a 1280x1024x3 offscreen buffer is ~3.9 MB): at 32 MiB the buffer could
/// not be allocated after a few app cycles (the first-fit allocator
/// fragments), so every tk redraw fell back to direct drawing and the
/// screen visibly flashed on each repaint.  96 MiB (0x6000000..0xC000000)
/// gives it comfortable headroom; the Rust heap was moved above it (192
/// MiB).  Both stay well inside the usable RAM of every supported config
/// (QEMU gates use -m 1G, VirtualBox 1024 MB, the launcher -m 512M; a bare
/// `qemu -m 128M` default would not have room for the enlarged heap).
pub const RUST_HEAP_PHYS: u64 = 0x1800_0000;
pub const RUST_HEAP_SIZE: usize = 64 * 1024 * 1024;
pub const C_HEAP_PHYS: u64 = 0x1000_0000;
// 128 MiB: 96 MiB could not satisfy a large image decode (~41 MiB peak for
// the stb path) once a media cycle had fragmented / pinned the heap; the
// region 0x1000_0000..0x1800_0000 is contiguous and stops exactly at the
// Rust heap, so this costs nothing and gives fragmentation headroom.
pub const C_HEAP_SIZE: usize = 128 * 1024 * 1024;

/// Log to both serial and VGA.
pub fn klog(s: &str) {
    serial::write_str(s);
    vga::write_str(s);
}

fn enable_sse() {
    unsafe {
        core::arch::asm!(
            "mov rax, cr0",
            "and rax, 0xFFFFFFFFFFFFFFFB",
            "or rax, 0x2",
            "mov cr0, rax",
            "mov rax, cr4",
            "or rax, 0x600",
            "mov cr4, rax",
            options(nomem, nostack, preserves_flags)
        );
    }
}

/// Raw byte out to COM1 as the very first instruction, before any init.
#[inline(never)]
fn probe_byte() {
    unsafe {
        core::arch::asm!(
            "mov dx, 0x3F8",
            "mov al, 0x21",
            "out dx, al",
            options(nomem, nostack)
        );
    }
}

#[inline(never)]
fn probe_char(c: u8) {
    unsafe {
        core::arch::asm!(
            "mov dx, 0x3F8",
            "mov al, {0}",
            "out dx, al",
            in(reg_byte) c,
            options(nomem, nostack)
        );
    }
}

#[inline(never)]
fn probe_heap(tag: u8) {
    probe_char(tag);
    serial::write_str("h=");
    serial::write_u64(libc::heap_start() as u64);
    serial::write_str(";");
}

fn kernel_main(boot_info: &'static mut bootloader_api::BootInfo) -> ! {
    probe_byte();
    enable_sse();
    probe_char(b'A');
    unsafe {
        idt::init();
        serial::init();
        pic::init();
        timer::init();
        keyboard::init();
        mouse::init();
    }
    probe_char(b'B');
    vga::clear();
    probe_char(b'C');
    klog("K0\n");
    probe_char(b'D');
    klog("K0\n");
    klog("Ripos: kernel core alive\n");
    klog("Ripos -- the Rust interpreted Python Operating System\n");

    let phys_offset = boot_info
        .physical_memory_offset
        .into_option()
        .unwrap_or(0) as usize;
    // Read everything we need from the boot info up front: the identity
    // map below remaps the range the boot info structure lives in.
    let regions = boot_info.memory_regions.len();
    probe_char(b'P');
    allocator::GLOBAL_ALLOCATOR.init(phys_offset + RUST_HEAP_PHYS as usize, RUST_HEAP_SIZE);
    klog("K1\n");
    klog("allocator selftest:\n");
    unsafe {
        allocator::GLOBAL_ALLOCATOR.selftest();
    }
    for (i, r) in boot_info.memory_regions.iter().enumerate() {
        klog(&alloc::format!("region[{}]: {:x}..{:x} {:?}\n", i, r.start, r.end, r.kind));
    }
    unsafe {
        libc::heap_init(phys_offset + C_HEAP_PHYS as usize, C_HEAP_SIZE);
    }
    // AC'97 audio controller (QEMU -device AC97 / VirtualBox AC97): real
    // PCM-out for the media player (kern.audio_*).  Runs after the Rust
    // allocator so its format! logging and heap DMA allocation are safe.
    audio::init(phys_offset as u64);
    // Phase 2: e1000 NIC + smoltcp TCP/IP (kern.net_*).  Polled from the
    // main loop below; only initialised when QEMU/VBox expose the device.
    net::init(phys_offset as u64);
    // Legacy: identity-map the low 1 GiB.  With the LP64 CPython build no
    // truncated pointers exist, but the mapping is harmless and kept for now.
    unsafe {
        page::map_low_1g_identity(phys_offset as u64);
    }
    // Initramfs-backed filesystem (M4): makes the pure-Python stdlib
    // importable via open/stat/readdir.
    unsafe {
        vfs::init();
    }
    // M6: hand the bootloader framebuffer to the driver.  The bootloader
    // maps it into the address space; Python draws into it zero-copy via
    // kern.fb_mem() (see drivers/framebuffer.py).
    match boot_info.framebuffer.take() {
        Some(fb) => {
            unsafe {
                framebuffer::init(fb, phys_offset as u64);
            }
        }
        None => klog("framebuffer: none (continuing headless)\n"),
    }
    probe_char(b'E');
    unsafe {
        libc::_tzname_init();
    }
    probe_heap(b'1');
    probe_char(b'F');
    klog(&alloc::format!(
        "heaps: rust {}M@0x{:x}, c {}M@0x{:x}, regions: {}\n",
        RUST_HEAP_SIZE / (1024 * 1024),
        RUST_HEAP_PHYS,
        C_HEAP_SIZE / (1024 * 1024),
        C_HEAP_PHYS,
        regions
    ));
    probe_heap(b'2');
    probe_char(b'H');

    sched::sched_init();
    probe_heap(b'3');
    klog("K2 scheduler up\n");

    klog("booting Python...\n");
    unsafe {
        pybind::pyboot_start();
    }
    probe_heap(b'4');
    if unsafe { pybind::Py_IsInitialized() } == 0 {
        klog("FATAL: Py_Initialize failed\n");
        loop {
            core::hint::spin_loop();
        }
    }
    klog("K3 Python initialized\n");

    // Enable interrupts now: the PIT drives preemptive slices (M5), the
    // timer callbacks, and keyboard IRQs.  Before any other thread exists
    // the timer ISR simply round-robins the single boot thread.
    unsafe {
        core::arch::asm!("sti", options(nomem, nostack));
    }

    unsafe {
        // Hold the GIL for the whole boot: with a huge eval-breaker switch
        // interval the kernel never drops/reacquires the GIL on TCG, which
        // occasionally hangs (condvar wake missed by the timer queue).  Do
        // this BEFORE the import self-check so hashlib/boot.py imports are
        // covered too (boot.py also raises it for the same reason).
        pybind::eval("import sys; sys.setswitchinterval(3600.0)", "<kernel>");
        pybind::eval("print('Python core alive')", "<kernel>");
        pybind::eval("print(40 + 2)", "<kernel>");
        pybind::eval(
            "import kern\n\
             kern.write('kern module loaded; tick=%d\\n' % kern.tick())\n\
             kern.after(1000, lambda: kern.write('timer callback after 1000ms\\n'))\n\
             kern.after(2000, lambda: kern.write('timer callback after 2000ms; alloc=%s\\n' % (kern.alloc_stats(),)))\n\
             def onkey(ev):\n\
             \x20   name, ch, pressed = ev\n\
             \x20   kern.write('key: %s ch=%r pressed=%r\\n' % (name, chr(ch) if ch else None, pressed))\n\
             kern.on_key(onkey)\n\
             kern.write('keyboard ready; type into the QEMU monitor: sendkey a\\n')",
            "<kernel>",
        );
        // Import capability self-check: frozen/builtin modules must work,
        // pure-Python stdlib needs a filesystem (M4).
        pybind::kern_import_selftest();
        // M5 demo (spawn/ps/kill process layer) is verified and recorded in
        // rec.txt; it is skipped at boot now because its busy-loop serial
        // flood costs minutes under TCG.  The process machinery stays:
        // `kern.spawn/ps/kill` + osproc.py are available to Python.
    }

    klog("Interpreter ready.\n");

    // M8 demo: the interactive Python REPL shell is the OS command line.
    // boot.py starts the shell; the main loop below services its key
    // events (import m7 from the shell boots the M7 window manager).
    unsafe {
        pybind::eval("import boot\n", "<kernel>");
    }

    // The main loop: service Python timers/key events, then yield.  With
    // preemption active the other threads (processes, the GIL breaker)
    // interleave with this loop automatically.
    loop {
        unsafe {
            // Recover keyboard bytes whose IRQ edge was lost (e.g. during a
            // cli window or a long synchronous eval): the 8042 output buffer
            // holds them, so decode any pending byte here.
            keyboard::kern_key_poll();
            pybind::kern_process_events();
        }
        // Phase 2: drive the network stack (ARP/IP/TCP timers + retransmit).
        net::poll();
        sched::sched_yield();
    }
}

#[panic_handler]
fn panic(info: &PanicInfo) -> ! {
    // No allocation: the panic may fire before the allocator is up (or from
    // the fault stack), and `alloc::format!` would recurse.
    klog("KERNEL PANIC: ");
    if let Some(s) = info.payload().downcast_ref::<&str>() {
        klog(s);
    } else {
        klog("(non-str payload)");
    }
    klog("\n");
    if let Some(loc) = info.location() {
        klog("  at ");
        klog(loc.file());
        klog(":");
        serial::write_u64(loc.line() as u64);
        klog("\n");
    }
    loop {
        core::hint::spin_loop();
    }
}

entry_point!(kernel_main, config = &BOOTLOADER_CONFIG);

// touch
// touch2
// touch3
// touch4
// touch5
// touch6
// touch7
// touch8
// touch9