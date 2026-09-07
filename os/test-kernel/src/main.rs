#![no_std]
#![no_main]

use core::panic::PanicInfo;
use limine::request::*;
use limine::{BaseRevision, RequestsEndMarker, RequestsStartMarker};

#[used]
#[unsafe(link_section = ".requests_start")]
pub static REQUESTS_START: RequestsStartMarker = RequestsStartMarker::new();

#[unsafe(link_section = ".requests")]
pub static BASE_REVISION: BaseRevision = BaseRevision::new();
#[unsafe(link_section = ".requests")]
pub static HHDM: HhdmRequest = HhdmRequest::new();
#[unsafe(link_section = ".requests")]
pub static STACK: StackSizeRequest = StackSizeRequest::new(1024 * 1024);
#[unsafe(link_section = ".requests")]
pub static ENTRY: EntryPointRequest = EntryPointRequest::new(_start);
#[used]
#[unsafe(link_section = ".requests_end")]
pub static REQUESTS_END: RequestsEndMarker = RequestsEndMarker::new();

unsafe fn outb(port: u16, val: u8) {
    core::arch::asm!("out dx, al", in("dx") port, in("al") val, options(nomem, nostack, preserves_flags));
}

unsafe fn serial_init() {
    unsafe { outb(0x3F8 + 1, 0x00) };
    unsafe { outb(0x3F8 + 3, 0x80) };
    unsafe { outb(0x3F8 + 0, 0x03) };
    unsafe { outb(0x3F8 + 1, 0x00) };
    unsafe { outb(0x3F8 + 3, 0x03) };
    unsafe { outb(0x3F8 + 2, 0xC7) };
    unsafe { outb(0x3F8 + 4, 0x0B) };
}

fn serial_str(s: &str) {
    for b in s.bytes() {
        unsafe {
            let mut status: u8;
            core::arch::asm!("in al, dx", out("al") status, in("dx") 0x3F8u16 + 5, options(nomem, nostack, preserves_flags));
            while status & 0x20 == 0 {
                core::arch::asm!("in al, dx", out("al") status, in("dx") 0x3F8u16 + 5, options(nomem, nostack, preserves_flags));
            }
            outb(0x3F8, b);
        }
    }
}

fn hex(v: u64) -> [u8; 18] {
    const HEX: &[u8; 16] = b"0123456789abcdef";
    let mut b = [b'0'; 18];
    b[0] = b'0';
    b[1] = b'x';
    for i in 0..16 {
        b[2 + i] = HEX[((v >> ((15 - i) * 4)) & 0xF) as usize];
    }
    b
}

#[unsafe(no_mangle)]
unsafe extern "C" fn _start() -> ! {
    serial_init();
    serial_str("MINIMAL LIMINE KERNEL ALIVE\n");
    if let Some(resp) = HHDM.response() {
        serial_str("hhdm=");
        let h = hex(resp.offset);
        serial_str(core::str::from_utf8(&h).unwrap());
        serial_str("\n");
    } else {
        serial_str("NO HHDM\n");
    }
    loop {
        core::hint::spin_loop();
    }
}

#[panic_handler]
fn panic(_info: &PanicInfo) -> ! {
    serial_str("MINI PANIC\n");
    loop {}
}
