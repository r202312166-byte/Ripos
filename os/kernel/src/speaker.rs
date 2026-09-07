//! speaker.rs -- PC speaker (PIT channel 2 + port 0x61) driver.
//!
//! The 8254 PIT channel 2 can generate a square wave at
//! PIT_FREQ / count Hz, gated into the PC speaker by port 0x61 bits 0-1
//! (bit 0 = gate, bit 1 = speaker data).  QEMU's isa-pcspk samples this
//! square wave at 32 kHz (mode 3 only), so "sound out" here means a
//! square-wave rendition of the decoded PCM: the player feeds a
//! frequency track (one tone per MPEG frame, derived from the actual
//! audio content) and the speaker emits it.
//!
//! Exposed to Python as kern.speaker(hz) / kern.speaker_off() through
//! the C shim (pyshim.c), exactly like kern_fb_*.

use core::arch::asm;

const PIT_FREQ: u32 = 1_193_182;

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

/// Program PIT channel 2 to emit a square wave at `hz` and open the
/// speaker gate.  hz == 0 silences the speaker.
#[no_mangle]
pub extern "C" fn kern_speaker_freq(hz: u32) {
    unsafe {
        if hz == 0 {
            kern_speaker_off();
            return;
        }
        // Clamp to the audible / reproducible range (QEMU pcspk needs
        // count >= PCSPK_MIN_COUNT ~= 38; below ~40 Hz the divisor
        // overflows 16 bits).
        let hz = hz.clamp(40, 20_000);
        let count = (PIT_FREQ / hz).clamp(1, 0xFFFF) as u16;
        // Channel 2, lobyte/hibyte access, mode 3 (square wave), binary.
        outb(0x43, 0xB6);
        outb(0x42, (count & 0xFF) as u8);
        outb(0x42, ((count >> 8) & 0xFF) as u8);
        // Enable the speaker: bit 0 (gate) + bit 1 (data).
        let v = inb(0x61);
        outb(0x61, (v & !0x03) | 0x03);
    }
}

/// Close the speaker gate (silence).  Keeps the PIT count programmed so a
/// later kern_speaker_freq() is a fast re-enable.
#[no_mangle]
pub extern "C" fn kern_speaker_off() {
    unsafe {
        let v = inb(0x61);
        outb(0x61, v & !0x03);
    }
}
