//! pci.rs -- minimal PCI bus driver (configuration space + BAR discovery).
//!
//! Only what the drivers need: enumerate bus 0, read/write config dwords,
//! find a device by vendor/device, read a BAR, enable bus mastering / I/O.
//! No dynamic allocation, no interrupt routing (drivers may poll).

use core::arch::asm;

const PCI_CONFIG_ADDR: u16 = 0x0CF8;
const PCI_CONFIG_DATA: u16 = 0x0CFC;

#[inline(always)]
unsafe fn outl(port: u16, val: u32) {
    unsafe {
        asm!("out dx, eax", in("dx") port, in("eax") val, options(nomem, nostack, preserves_flags));
    }
}

#[inline(always)]
unsafe fn inl(port: u16) -> u32 {
    let v: u32;
    unsafe {
        asm!("in eax, dx", out("eax") v, in("dx") port, options(nomem, nostack, preserves_flags));
    }
    v
}

fn config_addr(bus: u8, dev: u8, func: u8, reg: u8) -> u32 {
    0x8000_0000
        | ((bus as u32) << 16)
        | ((dev as u32) << 11)
        | ((func as u32) << 8)
        | ((reg as u32) & 0xFC)
}

/// Read a 32-bit config-space register.
pub fn read_dword(bus: u8, dev: u8, func: u8, reg: u8) -> u32 {
    unsafe {
        outl(PCI_CONFIG_ADDR, config_addr(bus, dev, func, reg));
        inl(PCI_CONFIG_DATA)
    }
}

/// Write a 32-bit config-space register.
pub fn write_dword(bus: u8, dev: u8, func: u8, reg: u8, val: u32) {
    unsafe {
        outl(PCI_CONFIG_ADDR, config_addr(bus, dev, func, reg));
        outl(PCI_CONFIG_DATA, val);
    }
}

/// Read a 16-bit config-space register (unaligned-safe: dword read + shift).
#[allow(dead_code)]
pub fn read_word(bus: u8, dev: u8, func: u8, reg: u8) -> u16 {
    let v = read_dword(bus, dev, func, reg & !3);
    ((v >> ((reg & 3) * 8)) & 0xFFFF) as u16
}

/// Find the first function with the given vendor/device on bus 0.
/// Returns (bus, device, function).
pub fn find(vendor: u16, device: u16) -> Option<(u8, u8, u8)> {
    for dev in 0..32u8 {
        let id = read_dword(0, dev, 0, 0);
        if id == 0xFFFF_FFFF || id == 0 {
            continue;
        }
        if (id & 0xFFFF) as u16 == vendor && ((id >> 16) & 0xFFFF) as u16 == device {
            return Some((0, dev, 0));
        }
    }
    None
}

/// Enable I/O space + memory space + bus mastering for a function.
pub fn enable(bus: u8, dev: u8, func: u8) {
    let cmd = read_dword(bus, dev, func, 0x04);
    write_dword(bus, dev, func, 0x04, cmd | 0x7);
}

/// Read a BAR (0..5).  Returns the raw 32-bit value; callers decode the
/// type bit (bit 0: I/O vs memory) and mask accordingly.
pub fn bar(bus: u8, dev: u8, func: u8, idx: u8) -> u32 {
    read_dword(bus, dev, func, 0x10 + idx * 4)
}

/// Interrupt line (0x3C) -- informational; drivers poll by default.
#[allow(dead_code)]
pub fn irq_line(bus: u8, dev: u8, func: u8) -> u8 {
    (read_dword(bus, dev, func, 0x3C) & 0xFF) as u8
}

/// Dump all bus-0 devices to the serial log (boot diagnostics).
#[allow(dead_code)]
pub fn dump() {
    use crate::klog;
    klog("PCI: scanning bus 0
");
    for dev in 0..32u8 {
        let id = read_dword(0, dev, 0, 0);
        if id == 0xFFFF_FFFF || id == 0 {
            continue;
        }
        let vendor = id & 0xFFFF;
        let device = (id >> 16) & 0xFFFF;
        let cls = read_dword(0, dev, 0, 0x08);
        klog(&crate::alloc::format!(
            "PCI: {:02x}:{:02x}.0 vendor={:04x} device={:04x} class={:06x}
",
            0,
            dev,
            vendor,
            device,
            cls >> 8
        ));
    }
}
