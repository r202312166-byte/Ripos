//! Minimal IDT: reports faults with RIP/CR2 over the serial console.
//! Long enough to debug the Python bootstrap; no interrupts are handled.

use core::arch::asm;

#[repr(C, packed)]
#[derive(Clone, Copy)]
struct IdtEntry {
    offset_low: u16,
    selector: u16,
    ist: u8,
    flags: u8,
    offset_mid: u16,
    offset_high: u32,
    zero: u32,
}

const IDT_LEN: usize = 256;

#[repr(C, align(16))]
struct Idt {
    entries: [IdtEntry; IDT_LEN],
}

static mut IDT: Idt = Idt {
    entries: [IdtEntry {
        offset_low: 0,
        selector: 0,
        ist: 0,
        flags: 0,
        offset_mid: 0,
        offset_high: 0,
        zero: 0,
    }; IDT_LEN],
};

// Vectors where the CPU pushes an error code before the frame.
const ERR_VECTORS: [u8; 8] = [8, 10, 11, 12, 13, 14, 17, 21];

/// #PF/#GP/#UD etc. entry: rdi = vector; then the saved regs + frame are on
/// the stack. We pass rsp to the handler and let it decode.

/// Dedicated stack for fault reporting: when the fault-time RSP is garbage
/// (e.g. a corrupted thread context), the CPU switches to this via IST[1]
/// BEFORE pushing the fault frame, so the dump always prints instead of
/// triple-faulting.  Must be mutable: an all-zero `static` would land in
/// .rodata and the stub would fault writing to it.
static mut FAULT_STACK: [u8; 8192] = [0; 8192];

/// Own GDT + TSS so exceptions can use an IST (the bootloader's GDT has no
/// usable TSS).  Selectors stay compatible with the bootloader's: CS=0x08,
/// DS/SS=0x10; the TSS is 0x18 (a 16-byte descriptor occupying entries 3-4).
static mut GDT: [u64; 6] = [0; 6];
static mut TSS: [u8; 104] = [0; 104];

/// Low 8 bytes of the 64-bit TSS system descriptor.
fn tss_descriptor_low(base: u64, limit: u32) -> u64 {
    let access = 0x89u64; // present, DPL0, 64-bit TSS available
    (limit as u64 & 0xFFFF)
        | ((base & 0xFFFFFF) << 16)
        | (access << 40)
        | (((limit >> 16) as u64 & 0xF) << 48)
        | (((base >> 24) & 0xFF) << 56)
}

/// Install a private GDT with a TSS, point IST[1] at the fault stack, and
/// mark every exception-vector IDT entry to use it.  Called after `lidt`.
/// Currently a no-op: the GDT/TSS work is a prime suspect for the Python
/// init regression, and the exception stubs already switch to the fault
/// stack manually.
pub unsafe fn setup_fault_ist() {
    unsafe {
        let _ = core::ptr::addr_of_mut!(FAULT_STACK);
        let _ = core::ptr::addr_of_mut!(TSS);
        let _ = core::ptr::addr_of_mut!(GDT);
    }
}

macro_rules! define_stub {
    ($name:ident, $vec:expr) => {
        #[unsafe(naked)]
        #[no_mangle]
        pub unsafe extern "C" fn $name() -> ! {
            // Naked: no compiler prologue, so the CPU fault frame sits
            // exactly at regs[15] (error vectors) / regs[14] (plain):
            //   regs[0..14] = r15 r14 r13 r12 rbp rbx r11 r10 r9 r8
            //                 rdi rsi rdx rcx rax (pushed)
            //   regs[15]    = [err] RIP
            //   regs[16..]  = [RIP] CS RFLAGS (no SS/RSP for CPL-0 faults)
            core::arch::naked_asm!(
                "cli",
                "push rax",
                "push rcx",
                "push rdx",
                "push rsi",
                "push rdi",
                "push r8",
                "push r9",
                "push r10",
                "push r11",
                "push rbx",
                "push rbp",
                "push r12",
                "push r13",
                "push r14",
                "push r15",
                "mov rsi, rsp",
                "mov edi, {v}",
                "call {h}",
                "2: jmp 2b",
                v = const $vec,
                h = sym exception_handler,
            );
        }
    };
}

define_stub!(stub_0, 0);
define_stub!(stub_1, 1);
define_stub!(stub_2, 2);
define_stub!(stub_3, 3);
define_stub!(stub_4, 4);
define_stub!(stub_5, 5);
define_stub!(stub_6, 6);
define_stub!(stub_7, 7);
define_stub!(stub_8, 8);
define_stub!(stub_9, 9);
define_stub!(stub_10, 10);
define_stub!(stub_11, 11);
define_stub!(stub_12, 12);
define_stub!(stub_13, 13);
define_stub!(stub_14, 14);
define_stub!(stub_16, 16);
define_stub!(stub_17, 17);
define_stub!(stub_18, 18);
define_stub!(stub_19, 19);
define_stub!(stub_20, 20);
define_stub!(stub_21, 21);
define_stub!(stub_30, 30);

/// IRQ entry: same register pushes as the exception stubs, but the handler
/// returns and we `iretq` back to the interrupted code (CPL-0: the CPU frame
/// is only RIP/CS/RFLAGS, no SS/RSP).
macro_rules! define_irq_stub {
    ($name:ident, $vec:expr) => {
        #[unsafe(naked)]
        #[no_mangle]
        pub unsafe extern "C" fn $name() -> ! {
            core::arch::naked_asm!(
                "cli",
                "push rax",
                "push rcx",
                "push rdx",
                "push rsi",
                "push rdi",
                "push r8",
                "push r9",
                "push r10",
                "push r11",
                "mov rsi, rsp",
                "mov edi, {v}",
                "call {h}",
                "pop r11",
                "pop r10",
                "pop r9",
                "pop r8",
                "pop rdi",
                "pop rsi",
                "pop rdx",
                "pop rcx",
                "pop rax",
                "iretq",
                v = const $vec,
                h = sym irq_handler,
            );
        }
    };
}

/// IRQ0 (PIT timer): plain stub.  The ISR only ticks, EOI's and fires
/// timed-out waiters; preemptive round-robin is disabled until the
/// cooperative path is stable.  The iretq resume is fine for the CPU-pushed
/// interrupt frame (the direct-jump resume proved unreliable here for the
/// crafted/cooperative frames in switch_to).
#[unsafe(naked)]
#[no_mangle]
pub unsafe extern "C" fn irq_0() -> ! {
    core::arch::naked_asm!(
        "cli",
        "push rax",
        "push rcx",
        "push rdx",
        "push rsi",
        "push rdi",
        "push r8",
        "push r9",
        "push r10",
        "push r11",
        "mov rsi, rsp",
        "mov edi, 32",
        "call {h}",
        "pop r11",
        "pop r10",
        "pop r9",
        "pop r8",
        "pop rdi",
        "pop rsi",
        "pop rdx",
        "pop rcx",
        "pop rax",
        "iretq",
        h = sym crate::sched::irq32_handler,
    );
}

define_irq_stub!(irq_1, 33);
define_irq_stub!(irq_2, 34);
define_irq_stub!(irq_3, 35);
define_irq_stub!(irq_4, 36);
define_irq_stub!(irq_5, 37);
define_irq_stub!(irq_6, 38);
define_irq_stub!(irq_7, 39);
define_irq_stub!(irq_8, 40);
define_irq_stub!(irq_9, 41);
define_irq_stub!(irq_10, 42);
define_irq_stub!(irq_11, 43);
define_irq_stub!(irq_12, 44);
define_irq_stub!(irq_13, 45);
define_irq_stub!(irq_14, 46);
define_irq_stub!(irq_15, 47);

/// IRQ dispatcher. Keep it minimal: bump the timer tick, read the keyboard,
/// EOI.  No allocation, no locks, no Python.
extern "C" fn irq_handler(vector: u32, _regs: *const u64) {
    match vector {
        32 => crate::timer::tick(),
        33 => crate::keyboard::irq(),
        44 => crate::mouse::irq(),
        _ => {}
    }
    unsafe {
        // Slave-PIC IRQs (vectors 40-47) need an EOI on both PICs.
        if vector >= 40 {
            crate::pic::eoi_slave();
        } else {
            crate::pic::eoi();
        }
    }
}

unsafe fn entry(handler: unsafe extern "C" fn() -> !) -> IdtEntry {
    let addr = handler as usize;
    IdtEntry {
        offset_low: (addr & 0xFFFF) as u16,
        selector: 0x8,
        ist: 0,
        flags: 0x8E,
        offset_mid: ((addr >> 16) & 0xFFFF) as u16,
        offset_high: ((addr >> 32) & 0xFFFF_FFFF) as u32,
        zero: 0,
    }
}

pub unsafe fn init() {
    unsafe {
        IDT.entries[0] = entry(stub_0);
        IDT.entries[1] = entry(stub_1);
        IDT.entries[2] = entry(stub_2);
        IDT.entries[3] = entry(stub_3);
        IDT.entries[4] = entry(stub_4);
        IDT.entries[5] = entry(stub_5);
        IDT.entries[6] = entry(stub_6);
        IDT.entries[7] = entry(stub_7);
        IDT.entries[8] = entry(stub_8);
        IDT.entries[9] = entry(stub_9);
        IDT.entries[10] = entry(stub_10);
        IDT.entries[11] = entry(stub_11);
        IDT.entries[12] = entry(stub_12);
        IDT.entries[13] = entry(stub_13);
        IDT.entries[14] = entry(stub_14);
        IDT.entries[16] = entry(stub_16);
        IDT.entries[17] = entry(stub_17);
        IDT.entries[18] = entry(stub_18);
        IDT.entries[19] = entry(stub_19);
        IDT.entries[20] = entry(stub_20);
        IDT.entries[21] = entry(stub_21);
        IDT.entries[30] = entry(stub_30);

        IDT.entries[32] = entry(irq_0);
        IDT.entries[33] = entry(irq_1);
        IDT.entries[34] = entry(irq_2);
        IDT.entries[35] = entry(irq_3);
        IDT.entries[36] = entry(irq_4);
        IDT.entries[37] = entry(irq_5);
        IDT.entries[38] = entry(irq_6);
        IDT.entries[39] = entry(irq_7);
        IDT.entries[40] = entry(irq_8);
        IDT.entries[41] = entry(irq_9);
        IDT.entries[42] = entry(irq_10);
        IDT.entries[43] = entry(irq_11);
        IDT.entries[44] = entry(irq_12);
        IDT.entries[45] = entry(irq_13);
        IDT.entries[46] = entry(irq_14);
        IDT.entries[47] = entry(irq_15);

        let idt_addr = core::ptr::addr_of!(IDT) as u64;
        let limit = (core::mem::size_of::<Idt>() - 1) as u16;
        asm!(
            "lidt [{}]",
            in(reg) &PseudoDesc { limit, base: idt_addr },
            options(readonly, nostack, preserves_flags)
        );
        setup_fault_ist();
    }
}

#[repr(C, packed(2))]
struct PseudoDesc {
    limit: u16,
    base: u64,
}

extern "C" fn exception_handler(vector: u32, regs: *const u64) {
    unsafe {
        // Naked stub layout (regs[0..14] = r15..rax pushed):
        //   error vectors: regs[15]=err regs[16]=RIP regs[17]=CS regs[18]=RFLAGS
        //   plain vectors: regs[15]=RIP regs[16]=CS regs[17]=RFLAGS
        // CPL-0 faults do not push SS/RSP; the fault-time RSP is
        // regs + (19 + e)*8 (15 saved regs + the CPU frame).
        let has_err = ERR_VECTORS.contains(&(vector as u8));
        let e = has_err as usize;
        let rip = *regs.add(15 + e);
        let err = if has_err { *regs.add(15) } else { 0 };
        let cs = *regs.add(16 + e);
        let rflags = *regs.add(17 + e);
        let fault_rsp = regs as u64 + (19 + e) as u64 * 8;
        let mut cr2: u64 = 0;
        asm!("mov {0}, cr2", out(reg) cr2, options(nomem, nostack));
        crate::serial::write_str("\nFAULT v=");
        crate::serial::write_u64(vector as u64);
        crate::serial::write_str(" rip=");
        crate::serial::write_u64(rip);
        crate::serial::write_str(" err=");
        crate::serial::write_u64(err);
        crate::serial::write_str(" cs=");
        crate::serial::write_u64(cs);
        crate::serial::write_str(" rflags=");
        crate::serial::write_u64(rflags);
        crate::serial::write_str(" rsp=");
        crate::serial::write_u64(fault_rsp);
        crate::serial::write_str(" cr2=");
        crate::serial::write_u64(cr2);
        crate::serial::write_str("\nstack:\n");
        for i in 0..24 {
            crate::serial::write_str("  [");
            crate::serial::write_u64(i as u64);
            crate::serial::write_str("] ");
            crate::serial::write_u64(*regs.add(i));
            crate::serial::write_str("\n");
        }
    }
}
