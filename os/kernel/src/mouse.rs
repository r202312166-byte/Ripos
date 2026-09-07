//! PS/2 mouse: 8042 aux-port init, IRQ12 handler and 3-byte packet decode.
//!
//! The ISR assembles the standard 3-byte mouse packets (flags, dx, dy) and
//! pushes a decoded (dx, dy, buttons) event into a fixed ring buffer; the
//! kernel main loop drains it and hands events to Python via
//! kern.mouse_events() / kern.on_mouse() in the C shim.  The ISR does no
//! allocation and takes no locks.
//!
//! Packet layout: byte0 = flags (bit0 left, bit1 right, bit2 middle pressed;
//! bit4 = x sign bit, bit5 = y sign bit, bit6/7 = x/y overflow), byte1 = x
//! delta (9-bit, sign bit in flags bit4), byte2 = y delta (9-bit).  Positive
//! x is right, positive y is DOWN (screen coordinates).

use core::arch::asm;
use core::ffi::{c_int, c_uchar};

const KB_DATA: u16 = 0x60;
const KB_STATUS: u16 = 0x64;
const KB_CMD: u16 = 0x64;

const EV_CAP: usize = 64;

/// One decoded mouse packet.  dx/dy are signed deltas in pixels (positive y
/// is down the screen); buttons is a bitmask (1=left, 2=right, 4=middle);
/// wheel is the signed wheel delta (0 when the wheel is not enabled).
#[repr(C)]
#[derive(Clone, Copy)]
pub struct MouseEvent {
    pub dx: i16,
    pub dy: i16,
    pub buttons: c_uchar,
    pub wheel: i16,
}

static mut EVENTS: [MouseEvent; EV_CAP] = [MouseEvent {
    dx: 0,
    dy: 0,
    buttons: 0,
    wheel: 0,
}; EV_CAP];
static mut EV_TAIL: usize = 0;   // write index (ISR only)
static mut EV_HEAD: usize = 0;   // read index (main loop only)
static mut PKT: [u8; 4] = [0; 4]; // partial packet assembly (ISR only)
static mut PKT_IDX: usize = 0;
static mut PKT_LEN: usize = 3;   // 3 bytes, or 4 once the wheel is enabled
static mut WHEEL: bool = false;  // IntelliMouse 4-byte mode negotiated
static mut INITIALIZED: bool = false;

unsafe fn inb(port: u16) -> u8 {
    let mut v: u8;
    unsafe {
        asm!("in al, dx", out("al") v, in("dx") port, options(nomem, nostack, preserves_flags));
    }
    v
}

unsafe fn outb(port: u16, val: u8) {
    unsafe {
        asm!("out dx, al", in("dx") port, in("al") val, options(nomem, nostack, preserves_flags));
    }
}

unsafe fn status() -> u8 {
    unsafe { inb(KB_STATUS) }
}

/// Wait until the 8042 output buffer has a byte, with a bounded spin.
unsafe fn wait_data() -> bool {
    unsafe {
        for _ in 0..100_000 {
            if status() & 1 != 0 {
                return true;
            }
        }
        false
    }
}

/// Wait until the 8042 input buffer is clear (controller ready for a write).
unsafe fn wait_ready() -> bool {
    unsafe {
        for _ in 0..100_000 {
            if status() & 2 == 0 {
                return true;
            }
        }
        false
    }
}

/// Send a command byte to the 8042 controller port.
unsafe fn cmd(b: u8) {
    unsafe {
        if wait_ready() {
            outb(KB_CMD, b);
        }
    }
}

/// Write a byte to the aux (mouse) device: prefix with 0xD4.
unsafe fn aux_write(b: u8) {
    unsafe {
        cmd(0xD4);
        if wait_ready() {
            outb(KB_DATA, b);
        }
    }
}

/// Send a byte to the aux device and return its ACK (0xFA) or 0 on timeout.
unsafe fn aux_write_ack(b: u8) -> u8 {
    unsafe {
        aux_write(b);
        if wait_data() {
            inb(KB_DATA)
        } else {
            0
        }
    }
}

/// 8042 + mouse init.  Called once at boot, after keyboard::init().
/// Enables the aux port and IRQ12, sets stream mode and enables data
/// reporting.  A missing mouse is tolerated (no events ever arrive).
pub unsafe fn init() {
    unsafe {
        // Flush any stale bytes.
        while status() & 1 != 0 {
            inb(KB_DATA);
        }
        // Enable the aux port.
        cmd(0xA8);
        // Read the 8042 command byte, set bit1 (aux interrupt enable) while
        // preserving bit0 (keyboard interrupt enable), write it back.
        cmd(0x20);
        if wait_data() {
            let mut cb = inb(KB_DATA);
            cb |= 0x02;
            cb |= 0x01;
            if wait_ready() {
                outb(KB_CMD, 0x60);
                if wait_ready() {
                    outb(KB_DATA, cb);
                }
            }
        }
        // Enable data reporting (0xF4); wait for the ACK.
        let ack = aux_write_ack(0xF4);
        if ack == 0xFA {
            INITIALIZED = true;
        } else {
            // Try again once (some controllers need a nudge).
            let ack2 = aux_write_ack(0xF4);
            INITIALIZED = ack2 == 0xFA;
        }
        // PS/2 IntelliMouse wheel: the magic 200/100/80 sample-rate
        // sequence switches the device to 4-byte packets with a signed
        // wheel delta in byte 3.  QEMU and most real mice honour it; a
        // plain 2-button device keeps sending 3-byte packets, which we
        // still decode (PKT_LEN falls back to 3 when the wheel never
        // appears).
        WHEEL = false;
        if INITIALIZED {
            let ok = aux_write_ack(0xF3) == 0xFA
                && aux_write_ack(0xC8) == 0xFA
                && aux_write_ack(0xF3) == 0xFA
                && aux_write_ack(0x64) == 0xFA
                && aux_write_ack(0xF3) == 0xFA
                && aux_write_ack(0x50) == 0xFA;
            WHEEL = ok;
            if WHEEL {
                crate::klog("mouse: PS/2 wheel enabled (4-byte packets)\n");
            }
        }
        PKT_IDX = 0;
        PKT_LEN = if WHEEL { 4 } else { 3 };
        EV_TAIL = 0;
        EV_HEAD = 0;
        if INITIALIZED {
            crate::klog("mouse: PS/2 mouse ready (IRQ12)\n");
        } else {
            crate::klog("mouse: no PS/2 mouse response (headless)\n");
        }
    }
}

unsafe fn push(ev: MouseEvent) {
    unsafe {
        let tail = EV_TAIL;
        let head = EV_HEAD;
        if (tail + 1) % EV_CAP == head {
            return; // full: drop
        }
        EVENTS[tail] = ev;
        EV_TAIL = (tail + 1) % EV_CAP;
    }
}

/// Called from the IRQ12 stub (vector 44).  Must be fast and lock-free.
pub fn irq() {
    unsafe {
        if status() & 1 != 0 {
            let b = inb(KB_DATA);
            if !INITIALIZED {
                return;
            }
            if PKT_IDX == 0 {
                // Flags byte: bit3 must be set for a valid packet start;
                // otherwise we are out of sync -- drop and resync.
                if b & 0x08 == 0 {
                    return;
                }
            }
            PKT[PKT_IDX] = b;
            PKT_IDX += 1;
            if PKT_IDX == PKT_LEN {
                PKT_IDX = 0;
                let flags = PKT[0];
                let xb = PKT[1];
                let yb = PKT[2];
                let mut dx = if flags & 0x10 != 0 { xb as i16 - 256 } else { xb as i16 };
                let mut dy = if flags & 0x20 != 0 { yb as i16 - 256 } else { yb as i16 };
                if flags & 0x40 != 0 {
                    dx = 0; // x overflow
                }
                if flags & 0x80 != 0 {
                    dy = 0; // y overflow
                }
                let wheel = if WHEEL && PKT_LEN == 4 {
                    (PKT[3] as i8) as i16
                } else {
                    0
                };
                push(MouseEvent {
                    dx,
                    dy,
                    buttons: flags & 0x07,
                    wheel,
                });
            }
        }
    }
}

/// Pop the next pending event (kernel main loop).  Returns 1 and fills
/// `out` on success.
#[no_mangle]
pub unsafe extern "C" fn kern_mouse_event_pop(out: *mut MouseEvent) -> c_int {
    unsafe {
        let head = EV_HEAD;
        if head == EV_TAIL {
            return 0;
        }
        *out = EVENTS[head];
        EV_HEAD = (head + 1) % EV_CAP;
        1
    }
}
