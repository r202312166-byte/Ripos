//! PS/2 keyboard: 8042 controller init, IRQ1 handler and scan-code decode
//! (set 1).  The ISR decodes each scancode and pushes a `KeyEvent` into a
//! fixed ring buffer; the kernel main loop drains it and hands events to
//! Python (see `kern.key_events()` / `kern.on_key()` in the C shim).
//! The ISR does no allocation and takes no locks.

use core::arch::asm;
use core::ffi::{c_char, c_int, c_uchar, CStr};

const KB_DATA: u16 = 0x60;
const KB_STATUS: u16 = 0x64;
const KB_CMD: u16 = 0x64;

const EV_CAP: usize = 64;

/// One decoded key event.  `name` is a static NUL-terminated string; `ch` is
/// the ASCII character (0 for non-printing keys); `pressed` is 1 on make.
#[repr(C)]
#[derive(Clone, Copy)]
pub struct KeyEvent {
    pub name: *const c_char,
    pub ch: c_uchar,
    pub pressed: c_uchar,
}

static mut EVENTS: [KeyEvent; EV_CAP] = [KeyEvent {
    name: core::ptr::null(),
    ch: 0,
    pressed: 0,
}; EV_CAP];
/// Write index (ISR only).
static mut EV_TAIL: usize = 0;
/// Read index (main loop only).
static mut EV_HEAD: usize = 0;
/// E0-prefix state (ISR only).
static mut EXTENDED: bool = false;
/// Left/right shift state (ISR only); makes ch carry uppercase/symbols.
static mut SHIFT: bool = false;

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

const fn cstr(s: &'static CStr) -> *const c_char {
    s.as_ptr() as *const c_char
}

// Name table indexed by make code (set 1, non-extended).  ch = ASCII or 0.
const KEY_TABLE: [(&'static CStr, char); 128] = [
    (c"", '\0'), (c"esc", '\0'), (c"1", '1'), (c"2", '2'), (c"3", '3'), (c"4", '4'), (c"5", '5'), (c"6", '6'),
    (c"7", '7'), (c"8", '8'), (c"9", '9'), (c"0", '0'), (c"-", '-'), (c"=", '='), (c"backspace", '\0'), (c"tab", '\t'),
    (c"q", 'q'), (c"w", 'w'), (c"e", 'e'), (c"r", 'r'), (c"t", 't'), (c"y", 'y'), (c"u", 'u'), (c"i", 'i'),
    (c"o", 'o'), (c"p", 'p'), (c"[", '['), (c"]", ']'), (c"enter", '\n'), (c"ctrl", '\0'), (c"a", 'a'), (c"s", 's'),
    (c"d", 'd'), (c"f", 'f'), (c"g", 'g'), (c"h", 'h'), (c"j", 'j'), (c"k", 'k'), (c"l", 'l'), (c";", ';'),
    (c"'", '\''), (c"`", '`'), (c"shift", '\0'), (c"\\", '\\'), (c"z", 'z'), (c"x", 'x'), (c"c", 'c'), (c"v", 'v'),
    (c"b", 'b'), (c"n", 'n'), (c"m", 'm'), (c",", ','), (c".", '.'), (c"/", '/'), (c"shift", '\0'), (c"*", '*'),
    (c"alt", '\0'), (c" ", ' '), (c"capslock", '\0'), (c"F1", '\0'), (c"F2", '\0'), (c"F3", '\0'), (c"F4", '\0'), (c"F5", '\0'),
    (c"F6", '\0'), (c"F7", '\0'), (c"F8", '\0'), (c"F9", '\0'), (c"F10", '\0'), (c"numlock", '\0'), (c"scrolllock", '\0'), (c"home", '\0'),
    (c"up", '\0'), (c"pgup", '\0'), (c"-", '-'), (c"left", '\0'), (c"5", '5'), (c"right", '\0'), (c"+", '+'), (c"end", '\0'),
    (c"down", '\0'), (c"pgdn", '\0'), (c"ins", '\0'), (c"del", '\0'), (c"", '\0'), (c"", '\0'), (c"", '\0'), (c"F11", '\0'),
    (c"F12", '\0'), (c"", '\0'), (c"", '\0'), (c"", '\0'), (c"", '\0'), (c"", '\0'), (c"", '\0'), (c"", '\0'),
    (c"", '\0'), (c"", '\0'), (c"", '\0'), (c"", '\0'), (c"", '\0'), (c"", '\0'), (c"", '\0'), (c"", '\0'),
    (c"", '\0'), (c"", '\0'), (c"", '\0'), (c"", '\0'), (c"", '\0'), (c"", '\0'), (c"", '\0'), (c"", '\0'),
    (c"", '\0'), (c"", '\0'), (c"", '\0'), (c"", '\0'), (c"", '\0'), (c"", '\0'), (c"", '\0'), (c"", '\0'),
    (c"", '\0'), (c"", '\0'), (c"", '\0'), (c"", '\0'), (c"", '\0'), (c"", '\0'), (c"", '\0'), (c"", '\0'),
];

// Shifted characters by make code (US layout): uppercase letters and the
// symbol row.  '\0' means "no change from the base table".  Only used for
// ch; the event name stays the unshifted key (e.g. name="9", ch='(').
const SHIFT_TABLE: [char; 128] = {
    let mut t = ['\0'; 128];
    // digits -> symbols
    t[0x02] = '!'; t[0x03] = '@'; t[0x04] = '#'; t[0x05] = '$'; t[0x06] = '%';
    t[0x07] = '^'; t[0x08] = '&'; t[0x09] = '*'; t[0x0A] = '('; t[0x0B] = ')';
    t[0x0C] = '_'; t[0x0D] = '+';
    // top letter row
    t[0x10] = 'Q'; t[0x11] = 'W'; t[0x12] = 'E'; t[0x13] = 'R'; t[0x14] = 'T';
    t[0x15] = 'Y'; t[0x16] = 'U'; t[0x17] = 'I'; t[0x18] = 'O'; t[0x19] = 'P';
    t[0x1A] = '{'; t[0x1B] = '}';
    // home row
    t[0x1E] = 'A'; t[0x1F] = 'S'; t[0x20] = 'D'; t[0x21] = 'F'; t[0x22] = 'G';
    t[0x23] = 'H'; t[0x24] = 'J'; t[0x25] = 'K'; t[0x26] = 'L';
    t[0x27] = ':'; t[0x28] = '\"'; t[0x29] = '~'; t[0x2B] = '|';
    // bottom letter row
    t[0x2C] = 'Z'; t[0x2D] = 'X'; t[0x2E] = 'C'; t[0x2F] = 'V'; t[0x30] = 'B';
    t[0x31] = 'N'; t[0x32] = 'M';
    t[0x33] = '<'; t[0x34] = '>'; t[0x35] = '?';
    t
};

// Extended (E0) make codes: (name, ch).
const EXT_TABLE: [(&'static CStr, char); 0x60] = [
    (c"", '\0'), (c"", '\0'), (c"", '\0'), (c"", '\0'), (c"", '\0'), (c"", '\0'), (c"", '\0'), (c"", '\0'),
    (c"", '\0'), (c"", '\0'), (c"", '\0'), (c"", '\0'), (c"", '\0'), (c"", '\0'), (c"", '\0'), (c"", '\0'),
    (c"", '\0'), (c"", '\0'), (c"", '\0'), (c"", '\0'), (c"", '\0'), (c"", '\0'), (c"", '\0'), (c"", '\0'),
    (c"", '\0'), (c"", '\0'), (c"", '\0'), (c"", '\0'), (c"enter", '\n'), (c"ctrl", '\0'), (c"", '\0'), (c"", '\0'),
    (c"", '\0'), (c"", '\0'), (c"", '\0'), (c"", '\0'), (c"", '\0'), (c"", '\0'), (c"", '\0'), (c"", '\0'),
    (c"", '\0'), (c"", '\0'), (c"", '\0'), (c"", '\0'), (c"", '\0'), (c"", '\0'), (c"", '\0'), (c"", '\0'),
    (c"", '\0'), (c"", '\0'), (c"", '\0'), (c"", '\0'), (c"", '\0'), (c"/", '/'), (c"", '\0'), (c"", '\0'),
    (c"alt", '\0'), (c"", '\0'), (c"", '\0'), (c"", '\0'), (c"", '\0'), (c"", '\0'), (c"", '\0'), (c"", '\0'),
    (c"", '\0'), (c"", '\0'), (c"", '\0'), (c"", '\0'), (c"", '\0'), (c"", '\0'), (c"", '\0'), (c"home", '\0'),
    (c"up", '\0'), (c"pgup", '\0'), (c"", '\0'), (c"left", '\0'), (c"", '\0'), (c"right", '\0'), (c"", '\0'), (c"end", '\0'),
    (c"down", '\0'), (c"pgdn", '\0'), (c"ins", '\0'), (c"del", '\0'), (c"", '\0'), (c"", '\0'), (c"", '\0'), (c"", '\0'),
    (c"", '\0'), (c"", '\0'), (c"", '\0'), (c"", '\0'), (c"", '\0'), (c"", '\0'), (c"", '\0'), (c"", '\0'),
];

/// 8042 + keyboard init: flush, enable the interface, ask the keyboard to
/// start scanning (bounded wait for the ACK), then IRQ1 is unmasked by the
/// PIC setup.
pub unsafe fn init() {
    unsafe {
        // Flush any stale scancode.
        while status() & 1 != 0 {
            inb(KB_DATA);
        }
        // Enable the keyboard interface.
        outb(KB_CMD, 0xAE);
        // Ask the keyboard to enable scanning (0xF4); wait for ACK 0xFA.
        outb(KB_DATA, 0xF4);
        for _ in 0..100_000 {
            if status() & 1 != 0 {
                let b = inb(KB_DATA);
                if b == 0xFA {
                    break;
                }
            }
        }
        EXTENDED = false;
        SHIFT = false;
        EV_TAIL = 0;
        EV_HEAD = 0;
    }
}

/// Push an event into the ring (ISR context).
unsafe fn push(ev: KeyEvent) {
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

/// Called from the IRQ1 stub (vector 33).  Must be fast and lock-free.
pub fn irq() {
    unsafe {
        // Only read the data port when the output buffer is full.
        if status() & 1 != 0 {
            let sc = inb(KB_DATA);
            if sc == 0xE0 {
                EXTENDED = true;
            } else if sc == 0xE1 {
                // Pause key: ignore.
            } else {
                let pressed = sc & 0x80 == 0;
                let code = (sc & 0x7F) as usize;
                // Track the shift keys so ch can carry uppercase/symbols.
                if code == 0x2A || code == 0x36 {
                    SHIFT = pressed;
                }
                let (name, ch) = if EXTENDED {
                    if code < EXT_TABLE.len() {
                        EXT_TABLE[code]
                    } else {
                        (c"", '\0')
                    }
                } else if code < KEY_TABLE.len() {
                    let (n, base) = KEY_TABLE[code];
                    let c = if SHIFT && code < SHIFT_TABLE.len()
                        && SHIFT_TABLE[code] != '\0'
                    {
                        SHIFT_TABLE[code]
                    } else {
                        base
                    };
                    (n, c)
                } else {
                    (c"", '\0')
                };
                EXTENDED = false;
                if name.to_bytes().is_empty() {
                    // Unknown code: still report it so the driver is visible.
                    push(KeyEvent {
                        name: cstr(c"scancode"),
                        ch: 0,
                        pressed: pressed as c_uchar,
                    });
                } else {
                    push(KeyEvent {
                        name: cstr(name),
                        ch: ch as u8,
                        pressed: pressed as c_uchar,
                    });
                }
            }
        }
    }
}

/// Pop the next pending event (kernel main loop).  Returns 1 and fills `out`
/// on success.
#[no_mangle]
pub unsafe extern "C" fn kern_key_event_pop(out: *mut KeyEvent) -> c_int {
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

/// Poll the 8042: if a keyboard byte is pending (its IRQ edge may have
/// been lost during a cli window), decode it now.  Called from the kernel
/// main loop so no scancode is ever stranded in the controller buffer
/// (which would wedge the PS/2 stream).
#[no_mangle]
pub unsafe extern "C" fn kern_key_poll() -> c_int {
    unsafe {
        if status() & 1 != 0 {
            irq();
            1
        } else {
            0
        }
    }
}

#[allow(unused)]
fn _static_asserts() {
    // c_char/c_uchar size sanity (host check only).
    let _ = core::mem::size_of::<KeyEvent>();
}