//! PIT (8254) timer: channel 0 as a 50 Hz rate generator.
//!
//! The ISR only bumps an atomic tick counter (interrupt-safe).  Python-level
//! timer callbacks are driven from the kernel main loop via
//! kern_process_timers in the C shim, never from interrupt context.
//!
//! The rate is deliberately LOW: every IRQ0 interrupts whatever the CPU is
//! doing (including long synchronous Python evals), and the interrupt
//! entry/exit path has historically been fragile in this kernel (rec.txt:
//! iretq misbehaves).  At 50 Hz the tick still advances in 20 ms steps and
//! kern_tick_ms() stays in milliseconds, but the exposure to IRQ-induced
//! corruption during app construction drops 20x.

use core::arch::asm;
use core::sync::atomic::{AtomicU64, Ordering};

/// Monotonic tick counter in milliseconds (advances 20 ms per IRQ0 at 50 Hz).
static TICKS_MS: AtomicU64 = AtomicU64::new(0);

const PIT_FREQ: u64 = 1_193_182;
const TARGET_HZ: u64 = 50;
const DIVISOR: u16 = (PIT_FREQ / TARGET_HZ) as u16; // ~23863
const MS_PER_TICK: u64 = 1000 / TARGET_HZ; // 20

pub unsafe fn init() {
    unsafe {
        // Channel 0, lobyte/hibyte access, mode 2 (rate generator), binary.
        asm!("out dx, al", in("dx") 0x43u16, in("al") 0x34u8, options(nomem, nostack, preserves_flags));
        asm!("out dx, al", in("dx") 0x40u16, in("al") (DIVISOR & 0xFF) as u8, options(nomem, nostack, preserves_flags));
        asm!("out dx, al", in("dx") 0x40u16, in("al") ((DIVISOR >> 8) & 0xFF) as u8, options(nomem, nostack, preserves_flags));
    }
}

/// Called from the IRQ0 stub; must stay tiny and lock-free.
pub fn tick() {
    TICKS_MS.fetch_add(MS_PER_TICK, Ordering::Relaxed);
}

/// Current uptime in milliseconds (exported to the C shim for kern.tick()).
#[no_mangle]
pub unsafe extern "C" fn kern_tick_ms() -> u64 {
    TICKS_MS.load(Ordering::Relaxed)
}
