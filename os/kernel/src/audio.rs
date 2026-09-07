//! audio.rs -- AC97 (Intel 82801AA, QEMU "AC97" / VirtualBox AC97) PCM-out.
//!
//! Polled bus-mastering DMA: a 32-entry descriptor ring (the hardware's
//! CIV/LVI are 5-bit, modulo 32) of 64 KiB buffers.  QEMU's AC97 uses
//! 8-byte buffer descriptors {addr, ctl_len}; ctl_len low 16 bits hold
//! byte length >> 1 and bit31 is IOC.
//!
//! DMA addressing: the kernel image is NOT identity-mapped (the bootloader
//! maps each segment at its virtual address while the file content lives at
//! kernel_offset + segment.offset), so a static's address must never be
//! handed to the controller.  The ring and the descriptor list are instead
//! allocated from the Rust heap (phys_offset window), where
//! physical = virtual - phys_offset -- the same scheme the e1000 driver
//! (nic.rs) uses.
//!
//! Register access sizes matter: QEMU dispatches on the access width, and
//! PO_BDBAR is only honoured as a 32-bit write, PO_LVI / PO_CR only as
//! byte writes (offsets below come from QEMU's qemu-ac97.c MKREGS layout:
//!   PO_BDBAR 0x10 (l), PO_CIV 0x14 (b, ro), PO_LVI 0x15 (b),
//!   PO_SR 0x16 (w; bit0 = SR_DCH underrun), PO_CR 0x1B (b; bit1 RR, bit0 run))
//!
//! Exposed to Python as kern.audio_ready() / audio_play() / audio_stop() /
//! audio_busy() through the C shim (pyshim.c).

use core::arch::asm;
use crate::pci;
use crate::klog;

const AC97_VENDOR: u16 = 0x8086;
const AC97_DEVICE: u16 = 0x2415; // 82801AA AC97 Audio

// AC'97 bus-mastering descriptor ring is 32 entries (CIV/LVI are 5-bit,
// modulo 32); using fewer breaks the CIV/LVI wrap arithmetic and stalls
// the DMA once the writer catches the hardware.
const NBLOCKS: usize = 32;         // DMA buffers (full hardware ring)
const BLOCK: usize = 65536;        // bytes per buffer (64 KiB, 64 KiB-aligned)
const RING_BYTES: usize = NBLOCKS * BLOCK;
const ALIGN: usize = 65536;

// NABM register offsets (QEMU ac97.c MKREGS(PO, 16))
const PO_BDBAR: u16 = 0x10;
const PO_CIV: u16 = 0x14;
const PO_LVI: u16 = 0x15;
const PO_SR: u16 = 0x16;
const PO_CR: u16 = 0x1B;
const SR_DCH: u16 = 0x0001;        // dma halted (underrun)

// QEMU AC97 buffer descriptor (8 bytes): { addr: u32, ctl_len: u32 }.
// ctl_len low 16 bits = byte length >> 1; bit31 = IOC; bit30 = BUP.
#[repr(C)]
#[derive(Clone, Copy)]
struct Bd {
    addr: u32,
    ctl_len: u32,
}

// DMA buffers (heap-allocated at boot; physical = virtual - phys_offset).
static mut RING_V: *mut u8 = core::ptr::null_mut();   // 64 KiB-aligned ring
static mut RING_PHYS: u64 = 0;
static mut BDL_V: *mut Bd = core::ptr::null_mut();
static mut BDL_PHYS: u64 = 0;

static mut MIXER: u16 = 0;         // NAMB base (I/O)
static mut NABM: u16 = 0;          // NABM base (I/O)
static mut RATE: u32 = 0;
static mut PLAYING: bool = false;
static mut WRITE_IDX: usize = 0;   // next buffer to fill (mod NBLOCKS)

#[inline(always)]
unsafe fn outb(port: u16, val: u8) {
    unsafe {
        asm!("out dx, al", in("dx") port, in("al") val, options(nomem, nostack, preserves_flags));
    }
}
#[inline(always)]
unsafe fn inb(port: u16) -> u8 {
    let v: u8;
    unsafe {
        asm!("in al, dx", out("al") v, in("dx") port, options(nomem, nostack, preserves_flags));
    }
    v
}
#[inline(always)]
unsafe fn outw(port: u16, val: u16) {
    unsafe {
        asm!("out dx, ax", in("dx") port, in("ax") val, options(nomem, nostack, preserves_flags));
    }
}
#[inline(always)]
unsafe fn inw(port: u16) -> u16 {
    let v: u16;
    unsafe {
        asm!("in ax, dx", out("ax") v, in("dx") port, options(nomem, nostack, preserves_flags));
    }
    v
}
#[inline(always)]
unsafe fn outl(port: u16, val: u32) {
    unsafe {
        asm!("out dx, eax", in("dx") port, in("eax") val, options(nomem, nostack, preserves_flags));
    }
}

/// Allocate zeroed DMA memory from the Rust heap; the caller converts the
/// virtual pointer to a physical address (ptr - phys_offset).
fn alloc_dma(bytes: usize) -> Option<*mut u8> {
    let mut v: alloc::vec::Vec<u8> = alloc::vec::Vec::new();
    v.resize(bytes, 0u8);
    let p = v.as_mut_ptr();
    core::mem::forget(v);
    if p.is_null() {
        None
    } else {
        Some(p)
    }
}

/// Virtual address of a ring block (for CPU-side fills).
fn block_virt(i: usize) -> usize {
    unsafe { RING_V as usize + i * BLOCK }
}

/// Physical address of a ring block (for the DMA descriptor).
fn block_phys(i: usize) -> u64 {
    unsafe { RING_PHYS + (i * BLOCK) as u64 }
}

/// Initialise the AC97 controller.  Returns true when a codec is present
/// and the PCM-out channel is programmed.  Safe to call once at boot.
pub fn init(phys_offset: u64) -> bool {
    let Some((bus, dev, func)) = pci::find(AC97_VENDOR, AC97_DEVICE) else {
        klog("audio: no AC97 (8086:2415) found\n");
        return false;
    };
    unsafe {
        pci::enable(bus, dev, func);
        let bar0 = pci::bar(bus, dev, func, 0);
        let bar1 = pci::bar(bus, dev, func, 1);
        if bar0 & 1 == 0 || bar1 & 1 == 0 {
            klog("audio: AC97 BARs are not I/O\n");
            return false;
        }
        let mixer = (bar0 & 0xFFFC) as u16;
        let nabm = (bar1 & 0xFFFC) as u16;
        MIXER = mixer;
        NABM = nabm;

        // Codec reset: write 0x1 to the Reset register, wait, then unmute.
        outw(mixer, 0x0001);
        let until = crate::time::monotonic_ns() + 50_000_000;
        while crate::time::monotonic_ns() < until {
            core::hint::spin_loop();
        }
        // Read back the Reset register: QEMU reports the (STAC9700) codec
        // ID here; logged but not treated as failure -- the WAV gate is the
        // real proof of audio.
        let id = inw(mixer);
        klog(&crate::alloc::format!("audio: codec reset id={:#x}\n", id));
        // Full volume, unmuted: master (0x02) and PCM out (0x18).
        outw(0x02 + mixer, 0x0000);
        outw(0x18 + mixer, 0x0000);
        // Rate is set on the first play (kern_audio_play).
        RATE = 0;
        WRITE_IDX = 0;
        PLAYING = false;

        // Allocate the DMA ring (64 KiB-aligned base) and descriptor list.
        let Some(ring_raw) = alloc_dma(RING_BYTES + ALIGN) else {
            klog("audio: cannot allocate DMA ring\n");
            return false;
        };
        let ring_v = (ring_raw as usize + ALIGN - 1) & !(ALIGN - 1);
        let ring_phys = ring_v as u64 - phys_offset;
        let Some(bdl_raw) = alloc_dma(NBLOCKS * 8) else {
            klog("audio: cannot allocate DMA descriptors\n");
            return false;
        };
        let bdl_v = bdl_raw as *mut Bd;
        let bdl_phys = bdl_v as u64 - phys_offset;
        RING_V = ring_v as *mut u8;
        RING_PHYS = ring_phys;
        BDL_V = bdl_v;
        BDL_PHYS = bdl_phys;

        // Build the descriptor list (8-byte BDs: addr + ctl_len).
        for i in 0..NBLOCKS {
            (*BDL_V.offset(i as isize)) = Bd {
                addr: block_phys(i) as u32,
                ctl_len: (BLOCK >> 1) as u32, // no IOC: keep IRQs out of this
            };
        }
        klog(&crate::alloc::format!(
            "audio: AC97 ready mixer={:#x} nabm={:#x} id={:#x} ring_v={:#x} ring_phys={:#x} bdl_phys={:#x}\n",
            mixer, nabm, id, ring_v, ring_phys, bdl_phys
        ));
        true
    }
}

fn set_rate(rate: u32) {
    let rate = rate.clamp(8000, 48000);
    if rate == unsafe { RATE } {
        return;
    }
    unsafe {
        // PCM Front DAC Rate (mixer reg 0x2C).
        outw(MIXER + 0x2C, rate as u16);
        RATE = rate;
    }
}

/// (Re)arm the PCM-out channel.  A full reset clears CIV and DMA-halt so
/// playback starts again from buffer 0.  Requires the descriptors to be
/// valid (they always are: lengths are set as buffers are pushed).
fn start() {
    unsafe {
        // Reset the channel: CR.RR (bit 1) -> CIV 0, DCH set, then run
        // (CR.RPBM bit 0) -> fetches BD 0 and starts DMA.
        outb(NABM + PO_CR, 0x02);
        // Reset clears BDBAR, so (re)program it as a 32-bit write.
        outl(NABM + PO_BDBAR, BDL_PHYS as u32);
        outb(NABM + PO_CR, 0x01);
        PLAYING = true;
    }
}

fn stop_hw() {
    unsafe {
        outb(NABM + PO_CR, 0x00);
        PLAYING = false;
    }
}

/// True when an AC97 codec is initialised.
#[no_mangle]
pub extern "C" fn kern_audio_ready() -> i32 {
    if unsafe { MIXER == 0 } {
        return 0;
    }
    1
}

/// Push PCM (16-bit LE, mono or stereo) into the DMA ring and play it.
/// volume is 0..=128 (128 = full).  Returns the number of bytes buffered,
/// or -1 when no audio device is present.
#[no_mangle]
pub extern "C" fn kern_audio_play(
    pcm: *const u8,
    len: i64,
    rate: i32,
    channels: i32,
    volume: i32,
) -> i64 {
    if unsafe { MIXER == 0 } || pcm.is_null() || len <= 0 {
        return -1;
    }
    if channels != 1 && channels != 2 {
        return -1;
    }
    let len = len as usize;
    let src = unsafe { core::slice::from_raw_parts(pcm, len) };
    let rate = rate as u32;
    let ch = channels as usize;
    let vol = volume.clamp(0, 128);

    set_rate(rate);

    // Copy into the ring, one 64 KiB block at a time, never overwriting the
    // buffer the hardware is currently playing (CIV).
    let mut off = 0usize;
    let mut written = 0usize;
    while off < len {
        let w = unsafe { WRITE_IDX % NBLOCKS };
        if unsafe { PLAYING } {
            // If the channel underran (SR.DCH) it stopped at LVI; the LVI
            // write below then re-arms it from the next valid descriptor.
            // Do NOT full-reset (start): that replays the whole ring from
            // BDL[0], which collides with the write pointer once the ring
            // is deeper than one buffer.
            let _sr = unsafe { inw(NABM + PO_SR) };
            let civ = unsafe { inb(NABM + PO_CIV) } as usize;
            if w == civ {
                break; // hardware is on this buffer; wait for the next tick
            }
        }
        let take = core::cmp::min(BLOCK, len - off);
        let dst = unsafe { core::slice::from_raw_parts_mut(block_virt(w) as *mut u8, BLOCK) };
        // Convert mono -> stereo and scale by volume.
        let mut d = 0usize;
        let mut s = off;
        let end = off + take;
        while s < end {
            let lo = src[s] as i32;
            let hi = if s + 1 < src.len() { src[s + 1] as i32 } else { 0 };
            let sample = (hi << 8) | lo; // 16-bit LE
            let scaled = ((sample * vol) >> 7).clamp(-32768, 32767);
            dst[d] = (scaled & 0xFF) as u8;
            dst[d + 1] = ((scaled >> 8) & 0xFF) as u8;
            if ch == 1 {
                // duplicate to the right channel
                dst[d + 2] = (scaled & 0xFF) as u8;
                dst[d + 3] = ((scaled >> 8) & 0xFF) as u8;
                d += 4;
            } else {
                d += 2;
            }
            s += 2;
        }
        // Update the descriptor length for this block (bytes >> 1).
        let dwords = d >> 1;
        unsafe {
            (*BDL_V.offset(w as isize)).addr = block_phys(w) as u32;
            (*BDL_V.offset(w as isize)).ctl_len = (dwords as u32) & 0xFFFF;
            // PO_LVI = this buffer is the new last-valid (byte write).
            outb(NABM + PO_LVI, w as u8);
        }
        off += take;
        written += take;
        unsafe { WRITE_IDX += 1 }
        if !unsafe { PLAYING } {
            start();
        }
    }
    written as i64
}

/// Stop playback and drain the ring.
#[no_mangle]
pub extern "C" fn kern_audio_stop() {
    if unsafe { MIXER == 0 } {
        return;
    }
    stop_hw();
    unsafe {
        WRITE_IDX = 0;
        for b in 0..NBLOCKS {
            let p = block_virt(b) as *mut u8;
            for i in 0..BLOCK {
                core::ptr::write_volatile(p.add(i), 0);
            }
        }
    }
}

/// Bytes currently buffered / in flight (approximate).
#[no_mangle]
pub extern "C" fn kern_audio_busy() -> i64 {
    if unsafe { MIXER == 0 } || !unsafe { PLAYING } {
        return 0;
    }
    unsafe {
        let civ = inb(NABM + PO_CIV) as i64;
        let last = (WRITE_IDX as i64 - 1).rem_euclid(NBLOCKS as i64);
        let in_flight = ((last - civ).rem_euclid(NBLOCKS as i64)) + 1;
        in_flight * BLOCK as i64
    }
}
