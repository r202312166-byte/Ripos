//! Framebuffer driver (M6): exposes the bootloader framebuffer to the C
//! shim so Python can draw pixels.
//!
//! The bootloader (bootloader crate 0.11) maps the framebuffer into the
//! address space and hands us its virtual address + geometry via BootInfo.
//! We store that here; the C shim turns it into `kern.fb_info()` /
//! `kern.fb_mem()` (a zero-copy writable memoryview), and the actual
//! drawing happens in Python (drivers/framebuffer.py).

use alloc::format;
use core::arch::asm;
use core::ffi::{c_int, c_void};

// Bochs/QEMU VBE interface: index/data ports for runtime mode changes.
// QEMU's default stdvga adapter exposes it (the bootloader's int 10h VBE
// mode set lands on the same hardware); VirtualBox's VMSVGA does not, so
// the kernel reports failure there and Python falls back to a virtual
// resolution.  The linear framebuffer base stays fixed (0xE0000000), so
// the already-mapped region remains valid as long as the new mode is not
// larger than the mapped size.
const VBE_DISPI_IO: u16 = 0x1CE;
const VBE_DISPI_DATA: u16 = 0x1CF;
const VBE_DISPI_INDEX_ID: u16 = 0x0;
const VBE_DISPI_INDEX_XRES: u16 = 0x1;
const VBE_DISPI_INDEX_YRES: u16 = 0x2;
const VBE_DISPI_INDEX_BPP: u16 = 0x3;
const VBE_DISPI_INDEX_ENABLE: u16 = 0x4;
const VBE_DISPI_DISABLED: u16 = 0x0;
const VBE_DISPI_ENABLED: u16 = 0x1;
const VBE_DISPI_LFB_ENABLED: u16 = 0x40;

unsafe fn outw(port: u16, val: u16) {
    unsafe {
        asm!("out dx, ax", in("dx") port, in("ax") val, options(nomem, nostack, preserves_flags));
    }
}

unsafe fn inw(port: u16) -> u16 {
    let mut v: u16;
    unsafe {
        asm!("in ax, dx", out("ax") v, in("dx") port, options(nomem, nostack, preserves_flags));
    }
    v
}

unsafe fn vbe_index(i: u16) -> u16 {
    unsafe {
        outw(VBE_DISPI_IO, i);
        inw(VBE_DISPI_DATA)
    }
}

unsafe fn vbe_write(i: u16, v: u16) {
    unsafe {
        outw(VBE_DISPI_IO, i);
        outw(VBE_DISPI_DATA, v);
    }
}

/// 1 if the Bochs VBE interface is present (QEMU stdvga), 0 otherwise.
pub unsafe fn bochs_present() -> bool {
    unsafe {
        let id = vbe_index(VBE_DISPI_INDEX_ID);
        (0xB0C0..=0xB0C5).contains(&id) || id == 0xB0C5
    }
}

static mut FB_ADDR: u64 = 0;
static mut FB_LEN: u64 = 0;
static mut FB_WIDTH: u32 = 0;
static mut FB_HEIGHT: u32 = 0;
static mut FB_STRIDE: u32 = 0;
static mut FB_BPP: u32 = 0;
static mut FB_FORMAT: u32 = 0;

/// Take the bootloader framebuffer and publish it to the C shim.
///
/// Format codes (shared with the Python driver):
///   0 = Bgr, 1 = Rgb, 2 = U8 (grayscale), 3 = Unknown.
pub unsafe fn init(fb: bootloader_api::info::FrameBuffer, phys_offset: u64) -> bool {
    unsafe {
        let info = fb.info();
        let buf: &'static mut [u8] = fb.into_buffer();
        FB_ADDR = buf.as_mut_ptr() as u64;
        FB_LEN = buf.len() as u64;
        // Extend the mapping so runtime Bochs-VBE mode changes can exceed
        // the boot mode (e.g. res 1920x1080 on QEMU stdvga / VBox with
        // enough video memory).  The extra pages are backed by the
        // adapter's linear-framebuffer BAR; a failure keeps the boot size.
        const FB_MAP_MAX: u64 = 16 * 1024 * 1024;
        if crate::page::extend_framebuffer_map(phys_offset, FB_ADDR, FB_LEN, FB_MAP_MAX) {
            FB_LEN = FB_MAP_MAX;
        }
        FB_WIDTH = info.width as u32;
        FB_HEIGHT = info.height as u32;
        FB_STRIDE = info.stride as u32;
        FB_BPP = info.bytes_per_pixel as u32;
        FB_FORMAT = match info.pixel_format {
            bootloader_api::info::PixelFormat::Bgr => 0,
            bootloader_api::info::PixelFormat::Rgb => 1,
            bootloader_api::info::PixelFormat::U8 => 2,
            _ => 3,
        };
        crate::klog(&format!(
            "framebuffer: {}x{} stride={} bpp={} fmt={} @0x{:x} ({} bytes)
",
            FB_WIDTH, FB_HEIGHT, FB_STRIDE, FB_BPP, FB_FORMAT, FB_ADDR, FB_LEN
        ));
        true
    }
}

/// 1 if a framebuffer was published, 0 otherwise.
#[no_mangle]
pub extern "C" fn kern_fb_present() -> c_int {
    unsafe {
        if FB_ADDR != 0 && FB_LEN != 0 {
            1
        } else {
            0
        }
    }
}

/// Geometry: width, height, stride (PIXELS, bootloader semantics -- the
/// Python driver multiplies by bytes-per-pixel for byte offsets),
/// bytes-per-pixel, format code.
#[no_mangle]
pub extern "C" fn kern_fb_get(
    w: *mut u32,
    h: *mut u32,
    stride: *mut u32,
    bpp: *mut u32,
    format: *mut c_int,
) {
    unsafe {
        if !w.is_null() {
            *w = FB_WIDTH;
        }
        if !h.is_null() {
            *h = FB_HEIGHT;
        }
        if !stride.is_null() {
            *stride = FB_STRIDE;
        }
        if !bpp.is_null() {
            *bpp = FB_BPP;
        }
        if !format.is_null() {
            *format = FB_FORMAT as c_int;
        }
    }
}

/// Mapped framebuffer address (kernel virtual address).
#[no_mangle]
pub extern "C" fn kern_fb_addr() -> *mut c_void {
    unsafe { FB_ADDR as *mut c_void }
}

/// Framebuffer size in bytes.
#[no_mangle]
pub extern "C" fn kern_fb_len() -> u64 {
    unsafe { FB_LEN }
}

/// Runtime display resolution change via the Bochs VBE interface (QEMU
/// stdvga).  Returns 1 and updates the published geometry on success; 0 if
/// the interface is absent, the mode is invalid, or the new mode would
/// exceed the currently mapped framebuffer (so Python falls back to a
/// virtual resolution).
#[no_mangle]
pub extern "C" fn kern_fb_set_mode(w: u32, h: u32) -> c_int {
    unsafe {
        if !bochs_present() {
            return 0;
        }
        if w == 0 || h == 0 || w > 0xFFFF || h > 0xFFFF {
            return 0;
        }
        let need = (w as u64) * (h as u64) * (FB_BPP as u64);
        if need > FB_LEN {
            return 0;
        }
        // Disable the display, program the new geometry, re-enable with the
        // linear framebuffer flag.
        vbe_write(VBE_DISPI_INDEX_ENABLE, VBE_DISPI_DISABLED);
        vbe_write(VBE_DISPI_INDEX_XRES, w as u16);
        vbe_write(VBE_DISPI_INDEX_YRES, h as u16);
        vbe_write(VBE_DISPI_INDEX_BPP, (FB_BPP * 8) as u16);
        vbe_write(
            VBE_DISPI_INDEX_ENABLE,
            VBE_DISPI_ENABLED | VBE_DISPI_LFB_ENABLED,
        );
        // Verify the mode actually took (read back the programmed size).
        let rw = vbe_index(VBE_DISPI_INDEX_XRES) as u32;
        let rh = vbe_index(VBE_DISPI_INDEX_YRES) as u32;
        if rw != w || rh != h {
            // Restore the previous geometry.
            vbe_write(VBE_DISPI_INDEX_ENABLE, VBE_DISPI_DISABLED);
            vbe_write(VBE_DISPI_INDEX_XRES, FB_WIDTH as u16);
            vbe_write(VBE_DISPI_INDEX_YRES, FB_HEIGHT as u16);
            vbe_write(VBE_DISPI_INDEX_BPP, (FB_BPP * 8) as u16);
            vbe_write(
                VBE_DISPI_INDEX_ENABLE,
                VBE_DISPI_ENABLED | VBE_DISPI_LFB_ENABLED,
            );
            return 0;
        }
        FB_WIDTH = w;
        FB_HEIGHT = h;
        FB_STRIDE = w;
        // Blank the visible area so stale pixels don't flash before Python
        // repaints.
        let n = (w as usize) * (h as usize) * (FB_BPP as usize);
        let base = FB_ADDR as *mut u8;
        for i in 0..n {
            *base.add(i) = 0;
        }
        crate::klog(&format!(
            "framebuffer: mode set -> {}x{} stride={} bpp={}\n",
            FB_WIDTH, FB_HEIGHT, FB_STRIDE, FB_BPP
        ));
        1
    }
}
