//! 8259 PIC: remap IRQs to IDT vectors 0x20..0x2F and provide EOI.
//! Unmasked: IRQ0 (PIT timer), IRQ1 (keyboard), IRQ2 (slave cascade) and
//! IRQ12 (PS/2 mouse on the slave PIC).

use core::arch::asm;

pub const IRQ0: u32 = 32; // timer (vector offset 0x20)

unsafe fn outb(port: u16, val: u8) {
    unsafe {
        asm!("out dx, al", in("dx") port, in("al") val, options(nomem, nostack, preserves_flags));
    }
}

pub unsafe fn init() {
    unsafe {
        // ICW1: initialize, edge triggered, cascade
        outb(0x20, 0x11);
        outb(0xA0, 0x11);
        // ICW2: vector offsets
        outb(0x21, 0x20); // master IRQs -> 0x20..0x27
        outb(0xA1, 0x28); // slave  IRQs -> 0x28..0x2F
        // ICW3: cascade wiring
        outb(0x21, 0x04); // slave on master IRQ2
        outb(0xA1, 0x02);
        // ICW4: 8086 mode
        outb(0x21, 0x01);
        outb(0xA1, 0x01);
        // OCW1: unmask IRQ0 (timer), IRQ1 (keyboard) and IRQ2 (slave
        // cascade) on the master; unmask IRQ12 (mouse) on the slave.
        outb(0x21, 0xF8);
        outb(0xA1, 0xEF);
    }
}

/// Send end-of-interrupt to the master PIC.
pub unsafe fn eoi() {
    unsafe {
        outb(0x20, 0x20);
    }
}

/// Send end-of-interrupt to the slave PIC (IRQs 8-15), then the master.
pub unsafe fn eoi_slave() {
    unsafe {
        outb(0xA0, 0x20);
        outb(0x20, 0x20);
    }
}
