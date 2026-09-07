pub const COM1: u16 = 0x3F8;

pub unsafe fn init() {
    // Disable all interrupts
    unsafe { port_out(COM1 + 1, 0x00) };
    // Enable DLAB (divisor latch access)
    unsafe { port_out(COM1 + 3, 0x80) };
    // Divisor 3 => 38400 baud
    unsafe { port_out(COM1 + 0, 0x03) };
    unsafe { port_out(COM1 + 1, 0x00) };
    // 8 bits, no parity, one stop bit
    unsafe { port_out(COM1 + 3, 0x03) };
    // Enable FIFO, clear them, 14-byte threshold
    unsafe { port_out(COM1 + 2, 0xC7) };
    // IRQs enabled, RTS/DSR set
    unsafe { port_out(COM1 + 4, 0x0B) };
}

unsafe fn port_out(port: u16, val: u8) {
    unsafe {
        core::arch::asm!("out dx, al", in("dx") port, in("al") val, options(nomem, nostack, preserves_flags));
    }
}

unsafe fn port_in(port: u16) -> u8 {
    let mut val: u8;
    unsafe {
        core::arch::asm!("in al, dx", out("al") val, in("dx") port, options(nomem, nostack, preserves_flags));
    }
    val
}

fn is_transmit_empty() -> bool {
    unsafe { port_in(COM1 + 5) & 0x20 != 0 }
}

pub fn write_byte(byte: u8) {
    while !is_transmit_empty() {}
    unsafe { port_out(COM1, byte) };
}

pub fn write_str(s: &str) {
    for b in s.bytes() {
        write_byte(b);
    }
}

pub fn write_bytes(bytes: &[u8]) {
    for b in bytes {
        write_byte(*b);
    }
}

const HEX: &[u8; 16] = b"0123456789abcdef";

pub fn write_u64(v: u64) {
    for shift in (0..16).rev() {
        let nibble = ((v >> (shift * 4)) & 0xF) as usize;
        write_byte(HEX[nibble]);
    }
}
