//! Time sources: TSC-based monotonic clock.

pub fn rdtsc() -> u64 {
    unsafe { core::arch::x86_64::_rdtsc() }
}

/// Monotonic time in nanoseconds.  TSC ticks are used as a nanosecond
/// approximation (QEMU TCG provides a stable-ish counter).
pub fn monotonic_ns() -> u64 {
    rdtsc()
}
