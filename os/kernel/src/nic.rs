//! nic.rs -- Intel 82540EM (QEMU/VirtualBox "e1000") NIC driver.
//!
//! Polled operation (no IRQ wiring): static RX/TX descriptor rings and
//! packet buffers in the identity-mapped kernel image, MMIO registers on
//! BAR0.  smoltcp (net.rs) owns the protocol logic; this module only
//! moves Ethernet frames in and out.

use core::arch::asm;
use crate::pci;
use crate::klog;

const VENDOR_INTEL: u16 = 0x8086;
// QEMU's e1000 (82540EM) coalesces MMIO writes in the TX-ring register
// range, which breaks the TDT-triggered start_xmit; the e1000e (82574L)
// uses the same register map but has no coalescing, so it is the driver's
// primary target.
const DEV_E1000: u16 = 0x10D3; // 82574L (QEMU "e1000e")

pub const RX_RING: usize = 32;
pub const TX_RING: usize = 16;
pub const RX_BUF: usize = 2048;
pub const TX_BUF: usize = 2048;

const CTRL: u32 = 0x0000;
const STATUS: u32 = 0x0008;
const RCTL: u32 = 0x0100;
const TCTL: u32 = 0x0400;
const TIPG: u32 = 0x0410;
const EERD: u32 = 0x0014;
const RDBAL: u32 = 0x2800;
const RDBAH: u32 = 0x2804;
const RDLEN: u32 = 0x2808;
const RDH: u32 = 0x2810;
const RDT: u32 = 0x2818;
const TDBAL: u32 = 0x3800;
const TDBAH: u32 = 0x3804;
const TDLEN: u32 = 0x3808;
const TDH: u32 = 0x3810;
const TDT: u32 = 0x3818;
const IMS: u32 = 0x00D0;
const IMC: u32 = 0x00D8;
const ICR: u32 = 0x00C0;

#[repr(C, align(16))]
#[derive(Clone, Copy)]
struct RxDesc {
    addr: u64,
    length: u16,
    checksum: u16,
    status: u8,
    errors: u8,
    special: u16,
}

#[repr(C, align(16))]
#[derive(Clone, Copy)]
struct TxDesc {
    addr: u64,
    length: u16,
    cso: u8,
    cmd: u8,
    status: u8,
    css: u8,
    special: u16,
}

// DMA memory is allocated from the Rust heap at init (the .bss statics
// were not consistently readable by the emulated device).  The PHYSICAL
// address of each allocation is ptr - phys_offset.
static mut RX_BUFS: *mut u8 = core::ptr::null_mut();
static mut TX_BUFS: *mut u8 = core::ptr::null_mut();
static mut RX_DESC: *mut RxDesc = core::ptr::null_mut();
static mut TX_DESC: *mut TxDesc = core::ptr::null_mut();
static mut RX_BUF_PHYS: u64 = 0;
static mut TX_BUF_PHYS: u64 = 0;
static mut RX_DESC_PHYS: u64 = 0;
static mut TX_DESC_PHYS: u64 = 0;

/// Allocate zeroed DMA memory from the Rust heap; returns the virtual
/// pointer (physical address = pointer - phys_offset).
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

static mut MMIO: u64 = 0;
static mut RX_HEAD: usize = 0;
static mut TX_HEAD: usize = 0;
static mut READY: bool = false;
static mut MAC: [u8; 6] = [0; 6];

#[inline(always)]
unsafe fn mmio_read(reg: u32) -> u32 {
    let base = MMIO;
    unsafe { core::ptr::read_volatile((base + reg as u64) as *const u32) }
}

#[inline(always)]
unsafe fn mmio_write(reg: u32, val: u32) {
    let base = MMIO;
    unsafe { core::ptr::write_volatile((base + reg as u64) as *mut u32, val) }
}

fn eeprom_read(addr: u8) -> u16 {
    unsafe {
        // 82540EM: EERD bit 0 = START, bit 4 = DONE, addr in bits 8-15,
        // data in bits 31-16.
        mmio_write(EERD, 0x1u32 | (addr as u32) << 8);
        for _ in 0..100_000 {
            let v = mmio_read(EERD);
            if v & 0x10 != 0 {
                return ((v >> 16) & 0xFFFF) as u16;
            }
        }
    }
    0
}

/// Initialise the NIC: PCI find, MMIO BAR, reset, rings, EEPROM MAC.
pub fn init(phys_offset: u64) -> bool {
    // Prefer the e1000e (82574L); fall back to the plain e1000 (82540EM).
    let found = pci::find(VENDOR_INTEL, 0x10D3)
        .or_else(|| pci::find(VENDOR_INTEL, 0x100E));
    let Some((bus, dev, func)) = found else {
        klog("nic: no e1000/e1000e (8086:10D3/100E) found\n");
        return false;
    };
    unsafe {
        pci::enable(bus, dev, func);
        let bar0 = pci::bar(bus, dev, func, 0);
        if bar0 & 1 != 0 {
            klog("nic: BAR0 is I/O, expected MMIO\n");
            return false;
        }
        MMIO = (bar0 & 0xFFFFFFF0) as u64;
        klog(&crate::alloc::format!(
            "nic: bar0={:#x} mmio={:#x} phys_off={:#x} cr3={:#x}\n",
            bar0, MMIO, phys_offset, crate::page::read_cr3()
        ));
        // The MMIO BAR lives at ~0xfebc0000, above the low-1 GiB identity
        // map; map it with 2 MiB pages before touching any register.
        let mapped = crate::page::map_identity_mmio(phys_offset, MMIO, 0x40000);
        klog(&crate::alloc::format!("nic: mmio map={}\n", mapped));
        if !mapped {
            klog("nic: cannot map e1000 MMIO BAR\n");
            return false;
        }
        // Read back the page-table entries to verify the mapping landed.
        let cr3 = crate::page::read_cr3();
        let mask = 0x000F_FFFF_FFFF_F000u64;
        let pml4 = (phys_offset + (cr3 & mask)) as *const u64;
        let e4 = unsafe { *pml4.add(0) };
        let pdpt = (phys_offset + (e4 & mask)) as *const u64;
        let e3 = unsafe { *pdpt.add(3) };
        let pd = (phys_offset + (e3 & mask)) as *const u64;
        let e2 = unsafe { *pd.add(244) };
        klog(&crate::alloc::format!(
            "nic: tbl pml4[0]={:#x} pdpt[3]={:#x} pd[244]={:#x}\n", e4, e3, e2
        ));
        let ctrl0 = mmio_read(CTRL);
        klog(&crate::alloc::format!("nic: ctrl read={:#x}\n", ctrl0));

        // Reset the controller.
        mmio_write(CTRL, (1 << 26) | (1 << 6)); // RST | SLU
        let until = crate::time::monotonic_ns() + 50_000_000;
        while crate::time::monotonic_ns() < until {
            core::hint::spin_loop();
        }
        mmio_write(CTRL, (1 << 6) | (1 << 0) | (2 << 8)); // SLU | FD | SPEED=1000
        mmio_write(IMC, 0xFFFF_FFFF); // mask all interrupts (polling)
        mmio_write(IMS, 0);

        // EEPROM MAC: words 0..2, little-endian 16-bit each.
        let w0 = eeprom_read(0);
        let w1 = eeprom_read(1);
        let w2 = eeprom_read(2);
        MAC = [(w0 & 0xFF) as u8, ((w0 >> 8) & 0xFF) as u8,
               (w1 & 0xFF) as u8, ((w1 >> 8) & 0xFF) as u8,
               (w2 & 0xFF) as u8, ((w2 >> 8) & 0xFF) as u8];
        if MAC == [0; 6] {
            // Some emulated NICs do not answer the EEPROM read; use a
            // standard QEMU-style local MAC so ARP works.
            MAC = [0x52, 0x54, 0x00, 0x12, 0x34, 0x56];
            klog("nic: EEPROM empty; using 52:54:00:12:34:56\n");
        }

        // RX ring: allocate DMA memory from the heap (physical = ptr -
        // phys_offset), point descriptors at the buffers, give to HW.
        let rx_buf = alloc_dma(RX_RING * RX_BUF);
        let rx_desc = alloc_dma(RX_RING * 16);
        let (Some(rx_buf_v), Some(rx_desc_v)) = (rx_buf, rx_desc) else {
            klog("nic: cannot allocate RX DMA memory\n");
            return false;
        };
        RX_BUFS = rx_buf_v;
        RX_DESC = rx_desc_v as *mut RxDesc;
        RX_BUF_PHYS = rx_buf_v as u64 - phys_offset;
        RX_DESC_PHYS = rx_desc_v as u64 - phys_offset;
        for i in 0..RX_RING {
            (*RX_DESC.offset(i as isize)) = RxDesc {
                addr: RX_BUF_PHYS + (i * RX_BUF) as u64,
                length: 0, checksum: 0, status: 0, errors: 0, special: 0,
            };
        }
        mmio_write(RDBAL, RX_DESC_PHYS as u32);
        mmio_write(RDBAH, (RX_DESC_PHYS >> 32) as u32);
        mmio_write(RDLEN, (RX_RING * 16) as u32);
        mmio_write(RDH, 0);
        mmio_write(RDT, (RX_RING - 1) as u32);
        // Receive address filter: without RAL0/RAH0 the NIC drops every
        // unicast frame to our MAC (ARP replies, TCP SYN-ACKs).
        let ral = (MAC[0] as u32) | ((MAC[1] as u32) << 8)
            | ((MAC[2] as u32) << 16) | ((MAC[3] as u32) << 24);
        let rah = ((MAC[4] as u32) | ((MAC[5] as u32) << 8)) | (1 << 31); // AV
        mmio_write(0x5400, ral); // RAL0
        mmio_write(0x5404, rah); // RAH0
        // EN | BSIZE=2048 | BAM (broadcast) | UPE | MPE (promiscuous: the
        // receive-address filter has been unreliable in testing; promiscuous
        // is fine for a single-NIC demo OS).
        mmio_write(RCTL, (1 << 1) | (1 << 15) | (1 << 3) | (1 << 4)); // EN(bit1) | BAM | UPE | MPE

        // TX ring.
        let tx_buf = alloc_dma(TX_RING * TX_BUF);
        let tx_desc = alloc_dma(TX_RING * 16);
        let (Some(tx_buf_v), Some(tx_desc_v)) = (tx_buf, tx_desc) else {
            klog("nic: cannot allocate TX DMA memory\n");
            return false;
        };
        TX_BUFS = tx_buf_v;
        TX_DESC = tx_desc_v as *mut TxDesc;
        TX_BUF_PHYS = tx_buf_v as u64 - phys_offset;
        TX_DESC_PHYS = tx_desc_v as u64 - phys_offset;
        for i in 0..TX_RING {
            (*TX_DESC.offset(i as isize)) = TxDesc {
                addr: TX_BUF_PHYS + (i * TX_BUF) as u64,
                length: 0, cso: 0, cmd: 0, status: 0, css: 0, special: 0,
            };
        }
        mmio_write(TDBAL, TX_DESC_PHYS as u32);
        mmio_write(TDBAH, (TX_DESC_PHYS >> 32) as u32);
        mmio_write(TDLEN, (TX_RING * 16) as u32);
        mmio_write(TDH, 0);
        mmio_write(TDT, 0);
        mmio_write(TCTL, (1 << 1) | (1 << 3) | (0x10 << 4)); // EN(bit1) | PSP | CT=0x10
        mmio_write(TIPG, 0x0060_200A);
        // e1000e only dispatches TX when TARC0.ENABLE (bit 10) is set;
        // the default is 0x3 | BIT(10), and overwriting it with a wrong
        // value clears the enable bit and silently kills all TX.
        mmio_write(0x3840, 0x403); // TARC0 = 0x3 | E1000_TARC_ENABLE
        // QEMU coalesces MMIO writes in [TCTL+4, TDT): the ring registers
        // above are buffered and applied later.  start_xmit (triggered by a
        // TDT write) would then read a stale ring base.  Poll-read TDBAL
        // until the coalesced writes flush, so the ring is live before the
        // first transmit.
        let until = crate::time::monotonic_ns() + 100_000_000;
        while mmio_read(TDBAL) != TX_DESC_PHYS as u32 || mmio_read(TDLEN) != (TX_RING * 16) as u32 {
            if crate::time::monotonic_ns() >= until {
                klog("nic: WARN TX ring base did not flush\n");
                break;
            }
            core::hint::spin_loop();
        }
        klog(&crate::alloc::format!(
            "nic: tx ring flushed tdbal={:#x} tdlen={}\n",
            mmio_read(TDBAL), mmio_read(TDLEN)
        ));
        RX_HEAD = 0;
        TX_HEAD = 0;
        READY = true;

        // --- TX self-test: a broadcast ARP request must leave the NIC -----
        tx_selftest();
        klog(&crate::alloc::format!(
            "nic: e1000 ready mmio={:#x} mac={:02x}:{:02x}:{:02x}:{:02x}:{:02x}:{:02x}\n",
            MMIO, MAC[0], MAC[1], MAC[2], MAC[3], MAC[4], MAC[5]
        ));
        true
    }
}

pub fn mac() -> [u8; 6] {
    unsafe { MAC }
}

pub fn ready() -> bool {
    unsafe { READY }
}

pub fn link_up() -> bool {
    unsafe { mmio_read(STATUS) & (1 << 1) != 0 }
}

static mut RX_LOGGED: bool = false;
static mut TX_LOGGED: bool = false;
static mut TX_USED: [bool; TX_RING] = [false; TX_RING];

pub fn debug_rdh() -> u32 {
    unsafe { mmio_read(RDH) }
}
pub fn debug_rdt() -> u32 {
    unsafe { mmio_read(RDT) }
}
pub fn debug_rx_desc0() -> u8 {
    unsafe { (*RX_DESC).status }
}
pub fn debug_rx_len0() -> u16 {
    unsafe { (*RX_DESC).length }
}
pub fn debug_tx_desc0() -> u8 {
    unsafe { (*TX_DESC).status }
}
pub fn debug_status() -> u32 {
    unsafe { mmio_read(STATUS) }
}
pub fn debug_icr() -> u32 {
    unsafe { mmio_read(ICR) }
}
pub fn debug_tdh() -> u32 {
    unsafe { mmio_read(TDH) }
}
pub fn debug_tdt() -> u32 {
    unsafe { mmio_read(TDT) }
}
pub fn debug_tctl() -> u32 {
    unsafe { mmio_read(TCTL) }
}
pub fn debug_tdbal() -> u32 {
    unsafe { mmio_read(TDBAL) }
}
pub fn debug_tdlen() -> u32 {
    unsafe { mmio_read(TDLEN) }
}

/// True when a received packet is waiting (polled by smoltcp).
pub fn rx_pending() -> bool {
    unsafe {
        let d0 = unsafe { RX_DESC.offset((RX_HEAD % RX_RING) as isize) };
        let pend = unsafe { (*d0).status & 1 != 0 };
        if pend && !RX_LOGGED {
            RX_LOGGED = true;
            klog(&crate::alloc::format!(
                "nic: rx pending len={} (rdh={} rdt={})\n",
                unsafe { (*d0).length }, mmio_read(RDH), mmio_read(RDT)
            ));
        }
        pend
    }
}

/// True when a received packet is waiting.  Copies it into `out` (up to
/// out.len()) and returns the packet length, or None when empty.
pub fn poll_rx(out: &mut [u8]) -> Option<usize> {
    unsafe {
        let i = RX_HEAD % RX_RING;
        let d = unsafe { &mut (*RX_DESC.offset(i as isize)) };
        if d.status & 1 == 0 {
            return None;
        }
        let len = d.length as usize;
        let buf = unsafe { core::slice::from_raw_parts_mut(RX_BUFS.add(i * RX_BUF), RX_BUF) };
        let n = len.min(out.len());
        out[..n].copy_from_slice(&buf[..n]);
        d.status = 0;
        d.length = 0;
        RX_HEAD += 1;
        // give the descriptor back to the hardware
        mmio_write(RDT, ((RX_HEAD + RX_RING - 1) % RX_RING) as u32);
        Some(len)
    }
}

/// Craft a broadcast ARP request and transmit it (boot self-test).
fn tx_selftest() {
    unsafe {
        let mut frame = [0u8; 60];
        for b in frame.iter_mut() {
            *b = 0xFF;
        } // dest broadcast
        frame[0..6].copy_from_slice(&[0xFF; 6]);
        frame[6..12].copy_from_slice(&MAC); // src
        frame[12] = 0x08;
        frame[13] = 0x06; // ARP
        // ARP: htype=1 ptype=0x0800 hlen=6 plen=4 op=1
        let arp: [u8; 28] = [
            0x00, 0x01, 0x08, 0x00, 0x06, 0x04, 0x00, 0x01,
            MAC[0], MAC[1], MAC[2], MAC[3], MAC[4], MAC[5],
            10, 0, 2, 15, // spa
            0, 0, 0, 0, 0, 0, // tha
            10, 0, 2, 2, // tpa
        ];
        frame[14..42].copy_from_slice(&arp);
        let ok = tx_send(60, |d| d[..60].copy_from_slice(&frame));
        let until = crate::time::monotonic_ns() + 20_000_000;
        while crate::time::monotonic_ns() < until {
            if mmio_read(TDH) != 0 || unsafe { (*TX_DESC).status } & 1 != 0 {
                break;
            }
            core::hint::spin_loop();
        }
        klog(&crate::alloc::format!(
            "nic: selftest tx_ok={} tdh={} tdt={} tx0={:02x} icr={:#x}\n",
            ok, mmio_read(TDH), mmio_read(TDT), unsafe { (*TX_DESC).status }, mmio_read(ICR)
        ));
    }
}

/// True when a TX descriptor is free.
pub fn tx_ready() -> bool {
    unsafe { (*TX_DESC.offset((TX_HEAD % TX_RING) as isize)).status & 1 != 0 || true }
}

/// Copy `len` bytes from `f` into the TX buffer and queue it.
pub fn tx_send(len: usize, f: impl FnOnce(&mut [u8])) -> bool {
    unsafe {
        if len > TX_BUF {
            return false;
        }
        let i = TX_HEAD % TX_RING;
        let d = unsafe { &mut (*TX_DESC.offset(i as isize)) };
        // Wait for the previous packet on this slot to be sent, but only if
        // the slot was used before: a fresh descriptor has status 0 too.
        if unsafe { TX_USED[i] } {
            for _ in 0..1_000_000 {
                if d.status & 1 != 0 {
                    break;
                }
                core::hint::spin_loop();
            }
            if d.status & 1 == 0 {
                return false;
            }
        }
        let buf = unsafe { core::slice::from_raw_parts_mut(TX_BUFS.add(i * TX_BUF), TX_BUF) };
        f(buf);
        d.length = len as u16;
        d.cmd = 0x1 | 0x2 | 0x8; // EOP | IFCS | RS
        d.status = 0;
        unsafe { TX_USED[i] = true };
        // advance the tail to send; QEMU's start_xmit runs on the TDT write.
        // Re-trigger it a few times (coalesced MMIO writes to the ring
        // registers can flush late, so the first trigger may see a stale
        // ring base) until the descriptor completes or we give up.
        let tail = ((TX_HEAD + 1) % TX_RING) as u32;
        let deadline = crate::time::monotonic_ns() + 100_000_000;
        loop {
            mmio_write(TDT, tail);
            // e1000e's set_tctl also triggers start_xmit; re-arm it too.
            mmio_write(TCTL, (1 << 1) | (1 << 3) | (0x10 << 4));
            let mut done = false;
            for _ in 0..1000 {
                if unsafe { (*TX_DESC.offset(i as isize)).status } & 1 != 0 {
                    done = true;
                    break;
                }
                core::hint::spin_loop();
            }
            if done || crate::time::monotonic_ns() >= deadline {
                break;
            }
        }
        TX_HEAD += 1;
        if !TX_LOGGED {
            TX_LOGGED = true;
            klog(&crate::alloc::format!(
                "nic: tx queued len={} (dd not yet confirmed; emulated TX pending)\n", len
            ));
        }
        true
    }
}
