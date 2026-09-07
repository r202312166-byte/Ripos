//! Cooperative single-CPU thread scheduler implementing the pthread subset
//! that CPython requires (GIL thread, condvars, TLS keys).
//!
//! ABI note: the CPython objects were compiled with `-mabi=sysv`, so all
//! pthread functions use the SysV C ABI.

use core::mem::size_of;
use core::ptr;
use core::sync::atomic::{AtomicUsize, Ordering};

pub const MAX_THREADS: usize = 64;
pub const MAX_TLS_KEYS: usize = 64;
pub const THREAD_STACK_SIZE: usize = 512 * 1024;

const TCB_RSP_OFF: usize = 0;
// Tcb layout must match the offsets used in switch_to (rsp at 0, gs_base
// at 56); keep both in sync with the struct definition below.
const TCB_GS_OFF: usize = 56;

/// Windows-style TEB emulation for CPython's native TLS.
///
/// The vendor CPython build declares two `__declspec(thread)` variables
/// (`_Py_tss_tstate` and `pkgcontext`) and the mingw CRT accesses them the
/// MSVC way: `mov %gs:0x58,%rdx` (TEB -> thread's TLS array), then
/// `(%rdx,%rax,8)` with `rax = _tls_index` (the image's TLS slot), giving
/// the thread's copy of the image `.tls$` section.  We fake that per thread:
///
/// ```text
///   +0x00..+0x58   zeroed (no SEH / stack cookie in use)
///   +0x58          qword: pointer to the TLS array (at +0x60)
///   +0x60..+0xA0   TLS array of 8 qwords; [0] = pointer to the TLS block
///   +0xA0..+0xB0   TLS block: copy of the image .tls$ template
/// ```
///
/// The image `.tls$` section is 16 bytes and both variables are NULL at
/// load, so a zeroed block is a faithful copy.
pub const TEB_REGION_SIZE: usize = 0x100;
pub const TEB_TLS_ARRAY_PTR_OFF: usize = 0x58;
pub const TEB_TLS_ARRAY_OFF: usize = 0x60;
pub const TEB_TLS_BLOCK_OFF: usize = 0xA0;
pub const TLS_BLOCK_SIZE: usize = 16;
pub const TLS_INDEX: usize = 0; // _tls_index for this image

#[repr(C)]
#[derive(Clone, Copy)]
pub struct Tcb {
    pub rsp: usize,
    pub stack_base: usize,
    pub stack_size: usize,
    pub state: usize,     // 0 = runnable, 1 = blocked, 2 = dead
    pub link: usize,      // run queue / cond wait list link (usize::MAX = end)
    pub entry: usize,
    pub arg: usize,
    pub gs_base: usize,   // per-thread TEB (see TEB layout above)
    pub tls: [usize; MAX_TLS_KEYS],
}

const TCB_ZERO: Tcb = Tcb {
    rsp: 0,
    stack_base: 0,
    stack_size: 0,
    state: 0,
    link: usize::MAX,
    entry: 0,
    arg: 0,
    gs_base: 0,
    tls: [0; MAX_TLS_KEYS],
};

/// The TCB array.  MUST be accessed only through tcbs_mut()/tcbs_ptr(),
/// which fetch the address via `sym` in inline asm: direct `static mut`
/// field access or `addr_of!` can resolve to a different copy of this
/// static, which would split the scheduler in two.
#[no_mangle]
static mut TCBS: [Tcb; MAX_THREADS] = [TCB_ZERO; MAX_THREADS];
static mut RUN_HEAD: usize = usize::MAX;
static mut RUN_TAIL: usize = usize::MAX;
static mut NEXT_TID: usize = 1;
static mut NEXT_KEY: usize = 1;

// Dead threads cannot free their own stack (they are running on it), so they
// park the pointer here; the next pthread_create drains it and returns the
// memory to the (now freeing) Rust allocator.
static mut DEAD_STACKS: [usize; MAX_THREADS] = [0; MAX_THREADS];
static mut DEAD_COUNT: usize = 0;

fn record_dead_stack(stack: usize) {
    unsafe {
        if DEAD_COUNT < MAX_THREADS {
            DEAD_STACKS[DEAD_COUNT] = stack;
            DEAD_COUNT += 1;
        }
    }
}

fn drain_dead_stacks() {
    unsafe {
        let layout =
            core::alloc::Layout::from_size_align(THREAD_STACK_SIZE, 16).expect("bad stack layout");
        for i in 0..DEAD_COUNT {
            let ptr = DEAD_STACKS[i] as *mut u8;
            if !ptr.is_null() {
                alloc::alloc::dealloc(ptr, layout);
            }
        }
        DEAD_COUNT = 0;
    }
}

static CURRENT: AtomicUsize = AtomicUsize::new(0);

fn cur() -> usize {
    CURRENT.load(Ordering::SeqCst)
}

/// Single access path for the TCB array.  Direct `static mut` field access
/// (and even `addr_of!`) can make the compiler materialize a *second copy*
/// of the static (edition-2024 static-mut handling), splitting the array
/// between the Rust code and the inline-asm switch.  Fetching the address
/// via `sym` in inline asm always yields the real symbol, so every access
/// goes through the same base.
#[inline]
unsafe fn tcbs_mut() -> *mut Tcb {
    let mut p: usize;
    core::arch::asm!("lea {0}, [rip + {s}]", out(reg) p, s = sym TCBS, options(nostack));
    p as *mut Tcb
}

#[inline]
unsafe fn tcbs_ptr() -> *const Tcb {
    tcbs_mut() as *const Tcb
}

/// Fault-time diagnostics: Rust view of the TCB array and the crafted frame.
#[no_mangle]
pub extern "C" fn sched_diag() {
    unsafe {
        let ao = tcbs_ptr() as usize;
        crate::serial::write_str("addr_of=");
        crate::serial::write_u64(ao as u64);
        crate::serial::write_str("\n");
        for slot in 0..3usize {
            crate::serial::write_str("t[");
            crate::serial::write_u64(slot as u64);
            crate::serial::write_str("]rsp=");
            crate::serial::write_u64((*tcbs_mut().add(slot)).rsp as u64);
            crate::serial::write_str(" ao=");
            crate::serial::write_u64(*((ao + slot * 0x240) as *const u64));
            crate::serial::write_str("\n");
        }
        let f = (*tcbs_mut().add(1)).rsp;
        crate::serial::write_str("fr=");
        crate::serial::write_u64(f as u64);
        crate::serial::write_str("\n");
        for i in 0..18 {
            crate::serial::write_u64(*((f + i * 8) as *const u64));
            crate::serial::write_str(" ");
        }
        crate::serial::write_str("\n");
    }
}

/// Diagnostic: print the rsp the switch just loaded plus the iret frame.
#[no_mangle]
pub extern "C" fn sched_load_probe(rsp: usize) {
    unsafe {
        crate::serial::write_str("ld=");
        crate::serial::write_u64(rsp as u64);
        crate::serial::write_str(" rip=");
        crate::serial::write_u64(*((rsp + 120) as *const u64));
        crate::serial::write_str(" cs=");
        crate::serial::write_u64(*((rsp + 128) as *const u64));
        crate::serial::write_str(" rf=");
        crate::serial::write_u64(*((rsp + 136) as *const u64));
        crate::serial::write_str("\n");
    }
}

/// Call switch_to with the TCBS/CURRENT addresses.  Both MUST come from the
/// raw-pointer helpers (or `addr_of!` at the *call site*): `sym` in inline
/// asm, or direct `static mut` field access elsewhere, can resolve to a
/// different copy of the static, splitting the TCB array in two.
///
/// Returns `()` (not `!`): a cooperative resume returns into the caller
/// after the call, so the compiler must emit the caller's real epilogue
/// (an `!` return type makes it emit `int3`, which the resumed thread
/// would execute as a breakpoint fault).
unsafe fn do_switch(next: usize) {
    unsafe {
        switch_to(
            next,
            tcbs_ptr() as usize,
            core::ptr::addr_of!(CURRENT) as usize,
        );
    }
}

unsafe fn push_run(slot: usize) {
    unsafe {
        (*tcbs_mut().add(slot)).state = 0;
        (*tcbs_mut().add(slot)).link = usize::MAX;
        if RUN_HEAD == usize::MAX {
            RUN_HEAD = slot;
            RUN_TAIL = slot;
        } else {
            (*tcbs_mut().add(RUN_TAIL)).link = slot;
            RUN_TAIL = slot;
        }
    }
}

unsafe fn pop_run() -> usize {
    unsafe {
        let h = RUN_HEAD;
        if h == usize::MAX {
            return usize::MAX;
        }
        RUN_HEAD = (*tcbs_mut().add(h)).link;
        if RUN_HEAD == usize::MAX {
            RUN_TAIL = usize::MAX;
        }
        (*tcbs_mut().add(h)).link = usize::MAX;
        h
    }
}

/// Set the GS segment base (MSR_GS_BASE).  The fake TEB lives at `base`.
pub unsafe fn set_gs_base(base: u64) {
    core::arch::asm!(
        "wrmsr",
        in("ecx") 0xC000_0101u32,
        in("eax") (base as u32),
        in("edx") ((base >> 32) as u32),
        options(nomem, nostack, preserves_flags)
    );
}

/// Allocate a zeroed fake-TEB/TLS region from the C heap and wire up the
/// pointers so that `mov %gs:0x58,%rdx; mov (%rdx,%rax,8),%rax` resolves to
/// the per-thread TLS block (image TLS slot 0).
unsafe fn alloc_tls_region() -> usize {
    let teb = crate::libc::malloc(TEB_REGION_SIZE) as usize;
    if teb == 0 {
        return 0;
    }
    ptr::write_bytes(teb as *mut u8, 0, TEB_REGION_SIZE);
    let array = teb + TEB_TLS_ARRAY_OFF;
    let block = teb + TEB_TLS_BLOCK_OFF;
    *((teb + TEB_TLS_ARRAY_PTR_OFF) as *mut usize) = array;
    *((array + TLS_INDEX * size_of::<usize>()) as *mut usize) = block;
    teb
}

/// Switch from the current thread to `next` using a *unified* context
/// format, so that cooperative switches, preemptive timer switches and
/// fresh-thread starts all resume through the same code:
///
/// ```text
///   [low] rax rcx rdx rsi rdi r8 r9 r10 r11 rbx rbp r12 r13 r14 r15
///   [high] RIP CS RFLAGS          (the CPL-0 iret frame)
/// ```
///
/// TCB.rsp points at rax (the lowest address).  Resuming a thread is:
/// `mov rsp, TCB[next].rsp; pop rax..r15; iretq`.  The timer ISR stub
/// produces exactly this layout (CPU pushes RIP/CS/RFLAGS, the stub pushes
/// the 15 GPRs), and pthread_create crafts it for new threads.
///
/// Interrupts are disabled for the whole switch; the saved RFLAGS has IF
/// forced on, so the resumed thread runs with interrupts enabled and the
/// timer ISR can never fire in the middle of a switch.
///
/// Naked: the compiler must not emit a prologue (it would shift the frame
/// by one slot).  `next` arrives in RDI, `tcbs` (the TCBS base address as
/// seen by Rust code) in RSI, `current` (the address of the CURRENT
/// atomic) in RDX.  The base addresses MUST come from Rust (`addr_of!`):
/// referencing `static mut` from `sym` in inline asm can resolve to a
/// different copy than the Rust compiler's constant, which would split the
/// TCB array in two.
///
/// The frame captures r8/r9/r10 holding (next, tcbs, current): caller-saved
/// registers whose pre-switch values are clobbered by any call anyway, so
/// the resumed thread never notices.
#[unsafe(naked)]
pub unsafe extern "C" fn switch_to(next: usize, tcbs: usize, current: usize) {
    core::arch::naked_asm!(
        "mov r9, rdi", // next  (caller-saved: safe to capture in the frame)
        "mov r8, rsi", // tcbs base (Rust-visible address)
        "mov r10, rdx", // current address
        "cli",
        // Build the iret frame below the GPRs, in CPU order (low to
        // high: RIP, CS, RFLAGS) -- pushed RFLAGS, CS, RIP.
        "pushfq",
        "push 0x8",
        "lea r11, [rip + 2f]",
        "push r11", // RIP = resume label
        // Save the current GPRs: r15 deepest, rax at rsp (this matches
        // the timer ISR stub, so resume is identical for both).
        "push r15",
        "push r14",
        "push r13",
        "push r12",
        "push rbp",
        "push rbx",
        "push r11",
        "push r10",
        "push r9",
        "push r8",
        "push rdi",
        "push rsi",
        "push rdx",
        "push rcx",
        "push rax",
        // Force IF=1 in the saved RFLAGS.  Layout from rsp:
        //   [rax..r15 at 0..120] [RIP at 120] [CS at 128] [RFLAGS at 136]
        // Clobbering r11 is fine: its value is already in the frame.
        "mov r11, qword ptr [rsp + 136]",
        "or r11, 0x200",
        "mov qword ptr [rsp + 136], r11",
        // TCB[source].rsp = rsp  (source = CURRENT, still the current thread)
        "mov rax, qword ptr [r10]",
        "imul rax, rax, {tcbsz}",
        "lea r11, [r8 + rax]",
        "mov [r11 + {rsp_off}], rsp",
        // CURRENT = next
        "mov qword ptr [r10], r9",
        // r11 = &(*tcbs_mut().add(next))
        "imul rax, r9, {tcbsz}",
        "lea r11, [r8 + rax]",
        // set GS base; load its rsp
        "mov rax, [r11 + {gs_off}]",
        "mov rdx, rax",
        "shr rdx, 32",
        "mov ecx, 0xC0000101",
        "wrmsr",
        "mov rsp, [r11 + {rsp_off}]",
        // Resume the target: pop the 15 GPRs, then jump to the saved RIP
        // with IF restored by popfq-ing the frame's own RFLAGS slot.  A
        // direct jump instead of iretq (iretq misbehaved here).  After the
        // pops, rsp points at [RIP][CS][RFLAGS]; popfq from +16 leaves rsp
        // exactly where iretq would (interrupted rsp / crafted top /
        // caller_ret) without pushing below the frame.
        "pop rax",
        "pop rcx",
        "pop rdx",
        "pop rsi",
        "pop rdi",
        "pop r8",
        "pop r9",
        "pop r10",
        "pop r11",
        "pop rbx",
        "pop rbp",
        "pop r12",
        "pop r13",
        "pop r14",
        "pop r15",
        "mov rax, qword ptr [rsp]",
        "add rsp, 16",
        "mov r11, qword ptr [rsp]",
        "or r11, 0x200",
        "mov qword ptr [rsp], r11",
        "popfq",
        "jmp rax",
        "2:",
        "ret",
        "2:",
        "ret", // return to switch_to's caller (rsp points at caller_ret)
        tcbsz = const size_of::<Tcb>(),
        rsp_off = const TCB_RSP_OFF,
        gs_off = const TCB_GS_OFF,
    );
}

/// Block the current thread; it must already be linked into a wait list.
/// Switches to the next runnable thread.  Runs with interrupts disabled
/// (the resumed thread gets IF=1 from its saved frame).
unsafe fn block() {
    unsafe {
        core::arch::asm!("cli", options(nomem, nostack));
        let cur_slot = cur();
        (*tcbs_mut().add(cur_slot)).state = 1;
        let next = pop_run();
        if next == usize::MAX {
            // No other runnable thread.  In practice the boot thread is
            // always runnable, so this should never happen; if it does,
            // re-enable interrupts and idle so timeouts can still fire.
            core::arch::asm!("sti", options(nomem, nostack));
            loop {
                core::hint::spin_loop();
            }
        }
        // switch_to updates CURRENT itself (after saving this frame).
        do_switch(next);
    }
}

/// Mark `slot` runnable again (must be blocked).
unsafe fn wake(slot: usize) {
    unsafe {
        if slot != usize::MAX && (*tcbs_mut().add(slot)).state == 1 {
            push_run(slot);
        }
    }
}

// ---------------------------------------------------------------------------
// Preemptive slices + wait timeouts (M5)
// ---------------------------------------------------------------------------
//
// The PIT (1000 Hz) IRQ0 ISR runs `kern_timer_isr`.  Every QUANTUM_TICKS it
// round-robins the run queue, saving the interrupted thread's frame and
// resuming the next thread's frame (both in the unified format above).  The
// same ISR fires cond_timedwait timeouts at 1 ms resolution, which is what
// makes CPython's GIL eval-breaker thread work.

const QUANTUM_TICKS: u32 = 5; // 5 ms slice at 1000 Hz
static mut QUANTUM_LEFT: u32 = QUANTUM_TICKS;

const MAX_TIMEOUTS: usize = 64;

#[derive(Clone, Copy)]
struct Timeout {
    deadline_ms: u64,
    slot: usize,
    active: bool,
}

static mut TIMEOUTS: [Timeout; MAX_TIMEOUTS] =
    [Timeout { deadline_ms: 0, slot: 0, active: false }; MAX_TIMEOUTS];

/// Register a wakeup for `slot` in `ms` milliseconds.  Returns 0 on success,
/// -1 when the table is full (caller then waits without a timeout).
pub unsafe fn kern_timeout_after(slot: usize, ms: u64) -> i32 {
    unsafe {
        for t in TIMEOUTS.iter_mut() {
            if !t.active {
                t.active = true;
                t.slot = slot;
                t.deadline_ms = crate::timer::kern_tick_ms() + ms;
                return 0;
            }
        }
        -1
    }
}

pub unsafe fn cancel_timeout(slot: usize) {
    unsafe {
        for t in TIMEOUTS.iter_mut() {
            if t.active && t.slot == slot {
                t.active = false;
            }
        }
    }
}

/// Called from the timer ISR every tick: wake threads whose deadline passed.
unsafe fn fire_due_timeouts() {
    unsafe {
        let now = crate::timer::kern_tick_ms();
        for t in TIMEOUTS.iter_mut() {
            if t.active && t.deadline_ms <= now {
                t.active = false;
                if (*tcbs_mut().add(t.slot)).state == 1 {
                    push_run(t.slot);
                }
            }
        }
    }
}

/// IRQ0 (timer) entry, called from the naked stub with all 15 GPRs pushed.
/// `frame_rsp` points at the saved r15; the stub resumes at the returned
/// frame.  Must not allocate, touch Python, or take any lock.
/// IRQ0 (PIT) handler for the plain timer stub: tick, EOI, fire timed-out
/// waiters.  No preemptive switching (see kern_timer_isr's comment).
#[no_mangle]
pub extern "C" fn irq32_handler(_vector: u32, _regs: *const u64) {
    unsafe {
        crate::timer::tick();
        crate::pic::eoi();
        fire_due_timeouts();
    }
}

#[no_mangle]
pub extern "C" fn kern_timer_isr(frame_rsp: usize) -> usize {
    unsafe {
        crate::timer::tick();
        crate::pic::eoi();
        fire_due_timeouts();
        // Preemptive round-robin is disabled for now: the timer ISR only
        // ticks and wakes timed-out waiters.  The cooperative scheduler
        // (sched_yield from mutex/sem busy-waits, kern.sleep, GIL switches)
        // interleaves threads; the preempt path needs more hardening before
        // it can run during Python init.
        let _ = frame_rsp;
        frame_rsp
    }
}

extern "C" fn thread_entry() {
    unsafe {
        let slot = cur();
        let f: extern "C" fn(*mut core::ffi::c_void) =
            core::mem::transmute((*tcbs_mut().add(slot)).entry);
        let arg = (*tcbs_mut().add(slot)).arg as *mut core::ffi::c_void;
        f(arg);
        // Thread finished: park its stack for reclaim, free its TLS region,
        // mark dead and switch away forever.
        record_dead_stack((*tcbs_mut().add(slot)).stack_base);
        crate::libc::free((*tcbs_mut().add(slot)).gs_base as *mut core::ffi::c_void);
        (*tcbs_mut().add(slot)).gs_base = 0;
        (*tcbs_mut().add(slot)).stack_base = 0;
        (*tcbs_mut().add(slot)).state = 2;
        core::arch::asm!("cli", options(nomem, nostack));
        let next = pop_run();
        if next == usize::MAX {
            loop {
                core::hint::spin_loop();
            }
        }
        // switch_to updates CURRENT itself (after saving this frame).
        do_switch(next);
    }
}

/// Initialize the scheduler with the current thread as thread 0.
pub fn sched_init() {
    unsafe {
        RUN_HEAD = usize::MAX;
        RUN_TAIL = usize::MAX;
        CURRENT.store(0, Ordering::SeqCst);
        (*tcbs_mut().add(0)) = TCB_ZERO;
        (*tcbs_mut().add(0)).state = 0;
        // Give the boot thread its TLS region and switch GS to it right
        // away; CPython reads %gs:0x58 on the first thread-state access.
        let teb = alloc_tls_region();
        (*tcbs_mut().add(0)).gs_base = teb;
        set_gs_base(teb as u64);
    }
}

fn sched_yield_inner() {
    unsafe {
        core::arch::asm!("cli", options(nomem, nostack));
        let cur_slot = cur();
        let next = pop_run();
        if next == usize::MAX || next == cur_slot {
            if next == cur_slot {
                push_run(cur_slot);
            }
            core::arch::asm!("sti", options(nomem, nostack));
            return;
        }
        push_run(cur_slot);
        // switch_to updates CURRENT itself (after saving this frame).
        do_switch(next);
    }
}

// ---------------------------------------------------------------------------
// pthread ABI
// ---------------------------------------------------------------------------

/// Opaque blob storage for a condvar.  In the vendor build's headers
/// pthread_cond_t is a 4-byte type and CPython's `pthread_lock` places it at
/// +4 inside a 12-byte object, so the pointer we receive is only 4-byte
/// aligned.  Store our state in the first 4 bytes and never touch more.
#[repr(C)]
pub struct Cond {
    pub head: u32, // waiter list head (thread slot) or u32::MAX
}

/// Opaque blob storage for a mutex.  Same story: pthread_mutex_t is 4 bytes
/// in the vendor build and sits at +8 inside CPython's `pthread_lock`.
/// `locked` doubles as the holder marker.
#[repr(C)]
pub struct Mutex {
    pub locked: u32,
}

#[no_mangle]
pub extern "C" fn sched_yield() -> i32 {
    sched_yield_inner();
    0
}

#[no_mangle]
pub extern "C" fn sched_setscheduler(_pid: i32, _policy: i32, _p: *const core::ffi::c_void) -> i32 {
    0
}

#[no_mangle]
pub extern "C" fn sched_getscheduler(_pid: i32) -> i32 {
    0
}

#[no_mangle]
pub extern "C" fn sched_getparam(_pid: i32, _param: *mut core::ffi::c_void) -> i32 {
    0
}

#[no_mangle]
pub extern "C" fn sched_setparam(_pid: i32, _param: *const core::ffi::c_void) -> i32 {
    0
}

#[no_mangle]
pub extern "C" fn sched_rr_get_interval(_pid: i32, _ts: *mut crate::libc::Timespec) -> i32 {
    unsafe {
        (*_ts).tv_sec = 0;
        (*_ts).tv_nsec = 100_000_000; // 100 ms
    }
    0
}

#[no_mangle]
pub extern "C" fn sched_getaffinity(
    _pid: i32,
    _setsize: usize,
    _set: *mut u8,
) -> i32 {
    // One CPU: bit 0 set.
    if _setsize > 0 {
        unsafe { *_set = 1 };
    }
    0
}

#[no_mangle]
pub extern "C" fn sched_setaffinity(
    _pid: i32,
    _setsize: usize,
    _set: *const u8,
) -> i32 {
    0
}

#[no_mangle]
pub extern "C" fn sched_get_priority_max(_policy: i32) -> i32 {
    0
}

#[no_mangle]
pub extern "C" fn sched_get_priority_min(_policy: i32) -> i32 {
    0
}

#[no_mangle]
pub extern "C" fn pthread_self() -> usize {
    cur()
}

#[no_mangle]
pub extern "C" fn pthread_create(
    thread: *mut usize,
    _attr: *mut core::ffi::c_void,
    start: extern "C" fn(*mut core::ffi::c_void),
    arg: *mut core::ffi::c_void,
) -> i32 {
    unsafe {
        // Reclaim stacks of dead threads now that the Rust allocator frees.
        drain_dead_stacks();
        // Find a free slot: one that has never been used (fresh slots have
        // stack_base == 0).  Slots of dead threads stay reserved until the
        // Rust side grows a real allocator able to reclaim stacks.
        let mut slot = usize::MAX;
        for i in 1..MAX_THREADS {
            if (*tcbs_mut().add(i)).stack_base == 0 {
                slot = i;
                break;
            }
        }
        if slot == usize::MAX {
            return 11; // EAGAIN
        }
        let layout = core::alloc::Layout::from_size_align(THREAD_STACK_SIZE, 16)
            .expect("bad stack layout");
        let stack = alloc::alloc::alloc(layout);
        if stack.is_null() {
            return 11;
        }
        let teb = alloc_tls_region();
        if teb == 0 {
            return 11;
        }
        // Unified context frame (see switch_to): 15 zero GPRs below the
        // iret frame [RIP=thread_entry][CS=0x8][RFLAGS=IF|bit1].  The
        // effective stack top is 8 mod 16: thread_entry is entered by the
        // resume jump (not a call), and the SysV ABI expects a callee's
        // entry rsp to be 8 mod 16 (the call would have pushed 8).  With a
        // 0-mod-16 top every aligned SSE store in the call chain below
        // faults (#GP on movaps to a misaligned frame slot).
        let top = stack as usize + THREAD_STACK_SIZE - 8;
        let saved_rsp = top - 144;
        for i in 0..15 {
            *((saved_rsp + i * 8) as *mut usize) = 0;
        }
        *((top - 24) as *mut usize) = thread_entry as usize;
        *((top - 16) as *mut usize) = 0x8;  // CS: kernel code
        *((top - 8) as *mut usize) = 0x202; // RFLAGS: IF=1 + reserved bit 1

        (*tcbs_mut().add(slot)) = TCB_ZERO;
        (*tcbs_mut().add(slot)).rsp = saved_rsp;
        (*tcbs_mut().add(slot)).stack_base = stack as usize;
        (*tcbs_mut().add(slot)).stack_size = THREAD_STACK_SIZE;
        (*tcbs_mut().add(slot)).entry = start as usize;
        (*tcbs_mut().add(slot)).arg = arg as usize;
        (*tcbs_mut().add(slot)).gs_base = teb;
        (*tcbs_mut().add(slot)).state = 0;
        core::arch::asm!("cli", options(nomem, nostack));
        push_run(slot);
        core::arch::asm!("sti", options(nomem, nostack));
        *thread = slot;
        0
    }
}

#[no_mangle]
pub extern "C" fn pthread_exit(_retval: *mut core::ffi::c_void) {
    unsafe {
        let slot = cur();
        record_dead_stack((*tcbs_mut().add(slot)).stack_base);
        crate::libc::free((*tcbs_mut().add(slot)).gs_base as *mut core::ffi::c_void);
        (*tcbs_mut().add(slot)).gs_base = 0;
        (*tcbs_mut().add(slot)).stack_base = 0;
        (*tcbs_mut().add(slot)).state = 2;
        core::arch::asm!("cli", options(nomem, nostack));
        let next = pop_run();
        if next == usize::MAX {
            loop {
                core::hint::spin_loop();
            }
        }
        // switch_to updates CURRENT itself (after saving this frame).
        do_switch(next);
    }
}

#[no_mangle]
pub extern "C" fn pthread_detach(_thread: usize) -> i32 {
    0
}

#[no_mangle]
pub extern "C" fn pthread_kill(_thread: usize, _sig: i32) -> i32 {
    0
}

#[no_mangle]
pub extern "C" fn pthread_attr_init(_attr: *mut core::ffi::c_void) -> i32 {
    0
}

#[no_mangle]
pub extern "C" fn pthread_attr_destroy(_attr: *mut core::ffi::c_void) -> i32 {
    0
}

#[no_mangle]
pub extern "C" fn pthread_attr_setscope(_attr: *mut core::ffi::c_void, _scope: i32) -> i32 {
    0
}

#[no_mangle]
pub extern "C" fn pthread_attr_setstacksize(
    _attr: *mut core::ffi::c_void,
    _size: usize,
) -> i32 {
    0
}

#[no_mangle]
pub extern "C" fn pthread_sigmask(
    _how: i32,
    _set: *const core::ffi::c_void,
    _old: *mut core::ffi::c_void,
) -> i32 {
    0
}

#[no_mangle]
pub extern "C" fn pthread_getcpuclockid(
    _thread: usize,
    clockid: *mut i32,
) -> i32 {
    // CLOCK_THREAD_CPUTIME_ID = 3
    unsafe { *clockid = 3 };
    0
}

// -- semaphores ---------------------------------------------------------
//
// musl sem_t is 32 bytes; CPython's pthread_lock embeds one.  We only
// touch the first 4 bytes (the count).  Real semantics are needed because
// Python's `threading.Lock` and the import machinery use sem_wait/post.

#[repr(C)]
pub struct Sem {
    pub count: i32,
}

const ETIMEDOUT: i32 = 110;
const EAGAIN_SEM: i32 = 11;

#[no_mangle]
pub extern "C" fn sem_init(sem: *mut Sem, _pshared: i32, value: u32) -> i32 {
    unsafe {
        (*sem).count = value as i32;
    }
    0
}

#[no_mangle]
pub extern "C" fn sem_destroy(_sem: *mut Sem) -> i32 {
    0
}

#[no_mangle]
pub extern "C" fn sem_wait(sem: *mut Sem) -> i32 {
    unsafe {
        loop {
            if (*sem).count > 0 {
                (*sem).count -= 1;
                return 0;
            }
            sched_yield_inner();
        }
    }
}

#[no_mangle]
pub extern "C" fn sem_trywait(sem: *mut Sem) -> i32 {
    unsafe {
        if (*sem).count > 0 {
            (*sem).count -= 1;
            return 0;
        }
        EAGAIN_SEM
    }
}

#[no_mangle]
pub extern "C" fn sem_post(sem: *mut Sem) -> i32 {
    unsafe {
        (*sem).count += 1;
    }
    0
}

#[no_mangle]
pub extern "C" fn sem_timedwait(
    sem: *mut Sem,
    abstime: *const crate::libc::Timespec,
) -> i32 {
    // abstime is CLOCK_REALTIME-based; our realtime clock starts at
    // 1.7e9 + monotonic_ns().
    unsafe {
        let target = (*abstime).tv_sec as u64 * 1_000_000_000 + (*abstime).tv_nsec as u64;
        loop {
            if (*sem).count > 0 {
                (*sem).count -= 1;
                return 0;
            }
            let now = 1_700_000_000u64 * 1_000_000_000 + crate::time::monotonic_ns();
            if now >= target {
                return ETIMEDOUT;
            }
            sched_yield_inner();
        }
    }
}

// -- mutex -------------------------------------------------------------

#[no_mangle]
pub extern "C" fn pthread_mutex_init(
    mutex: *mut Mutex,
    _attr: *mut core::ffi::c_void,
) -> i32 {
    unsafe {
        (*mutex).locked = 0;
    }
    0
}

#[no_mangle]
pub extern "C" fn pthread_mutex_destroy(_mutex: *mut Mutex) -> i32 {
    0
}

#[no_mangle]
pub extern "C" fn pthread_mutex_lock(mutex: *mut Mutex) -> i32 {
    unsafe {
        loop {
            if (*mutex).locked == 0 {
                (*mutex).locked = 1;
                return 0;
            }
            sched_yield_inner();
        }
    }
}

#[no_mangle]
pub extern "C" fn pthread_mutex_trylock(mutex: *mut Mutex) -> i32 {
    unsafe {
        if (*mutex).locked == 0 {
            (*mutex).locked = 1;
            return 0;
        }
        16 // EBUSY
    }
}

#[no_mangle]
pub extern "C" fn pthread_mutex_unlock(mutex: *mut Mutex) -> i32 {
    unsafe {
        (*mutex).locked = 0;
    }
    0
}

// -- condvar -----------------------------------------------------------

#[no_mangle]
pub extern "C" fn pthread_cond_init(
    cond: *mut Cond,
    _attr: *mut core::ffi::c_void,
) -> i32 {
    unsafe {
        (*cond).head = u32::MAX;
    }
    0
}

#[no_mangle]
pub extern "C" fn pthread_cond_destroy(_cond: *mut Cond) -> i32 {
    0
}

#[no_mangle]
pub extern "C" fn pthread_condattr_init(_a: *mut core::ffi::c_void) -> i32 {
    0
}

#[no_mangle]
pub extern "C" fn pthread_condattr_destroy(_a: *mut core::ffi::c_void) -> i32 {
    0
}

#[no_mangle]
pub extern "C" fn pthread_condattr_setclock(_a: *mut core::ffi::c_void, _c: i32) -> i32 {
    0
}

unsafe fn cond_wait_internal(cond: *mut Cond, mutex: *mut Mutex) {
    unsafe {
        core::arch::asm!("cli", options(nomem, nostack));
        let me = cur();
        // Link self into the waiter list.
        (*tcbs_mut().add(me)).link = (*cond).head as usize;
        (*cond).head = me as u32;
        // Release the mutex.
        (*mutex).locked = 0;
        // Block (switches away; resumes with IF=1 from the saved frame).
        block();
        // Re-enter the critical section and re-acquire the mutex.
        core::arch::asm!("cli", options(nomem, nostack));
        loop {
            if (*mutex).locked == 0 {
                (*mutex).locked = 1;
                break;
            }
            sched_yield_inner();
        }
        core::arch::asm!("sti", options(nomem, nostack));
    }
}

#[no_mangle]
pub extern "C" fn pthread_cond_wait(cond: *mut Cond, mutex: *mut Mutex) -> i32 {
    unsafe { cond_wait_internal(cond, mutex) }
    0
}

#[no_mangle]
pub extern "C" fn pthread_cond_timedwait(
    cond: *mut Cond,
    mutex: *mut Mutex,
    abstime: *const crate::libc::Timespec,
) -> i32 {
    unsafe {
        if abstime.is_null() {
            return pthread_cond_wait(cond, mutex);
        }
        // The deadline may be CLOCK_REALTIME-based (threading lock timeouts;
        // our realtime = 1.7e9 s + monotonic_ns) or CLOCK_MONOTONIC-based
        // (CPython's GIL switch cond).  Values with tv_sec < 1e6 are
        // clearly monotonic; the realtime base is ~1.7e9 s.
        let realtime_based = (*abstime).tv_sec >= 1_000_000;
        let target = (*abstime).tv_sec as u64 * 1_000_000_000 + (*abstime).tv_nsec as u64;
        let now = if realtime_based {
            1_700_000_000u64 * 1_000_000_000 + crate::time::monotonic_ns()
        } else {
            crate::time::monotonic_ns()
        };
        if now >= target {
            return ETIMEDOUT;
        }
        let me = cur();
        let ms = (target - now) / 1_000_000 + 1;
        let has_timeout = kern_timeout_after(me, ms) == 0;
        core::arch::asm!("cli", options(nomem, nostack));
        (*tcbs_mut().add(me)).link = (*cond).head as usize;
        (*cond).head = me as u32;
        (*mutex).locked = 0;
        block();
        // Woken by a signal or the timeout.  A signal unlinked us (link =
        // MAX); a timeout left us linked.
        core::arch::asm!("cli", options(nomem, nostack));
        let timed_out = (*tcbs_mut().add(me)).link != usize::MAX;
        if timed_out {
            (*tcbs_mut().add(me)).link = usize::MAX; // remove ourselves from the wait list
        } else if has_timeout {
            cancel_timeout(me);
        }
        loop {
            if (*mutex).locked == 0 {
                (*mutex).locked = 1;
                break;
            }
            sched_yield_inner();
        }
        core::arch::asm!("sti", options(nomem, nostack));
        if timed_out {
            ETIMEDOUT
        } else {
            0
        }
    }
}

#[no_mangle]
pub extern "C" fn pthread_cond_timedwait64(
    cond: *mut Cond,
    mutex: *mut Mutex,
    abstime: *const crate::libc::Timespec,
) -> i32 {
    pthread_cond_timedwait(cond, mutex, abstime)
}

#[no_mangle]
pub extern "C" fn pthread_cond_timedwait32(
    cond: *mut Cond,
    mutex: *mut Mutex,
    abstime: *const crate::libc::Timespec,
) -> i32 {
    pthread_cond_timedwait(cond, mutex, abstime)
}

unsafe fn cond_signal_internal(cond: *mut Cond, broadcast: bool) {
    unsafe {
        core::arch::asm!("cli", options(nomem, nostack));
        let mut head = (*cond).head as usize;
        while head != u32::MAX as usize {
            let next = (*tcbs_mut().add(head)).link;
            (*tcbs_mut().add(head)).link = usize::MAX;
            if (*tcbs_mut().add(head)).state == 1 {
                push_run(head);
            }
            head = next;
            if !broadcast {
                break;
            }
        }
        (*cond).head = if broadcast { u32::MAX } else { head as u32 };
        core::arch::asm!("sti", options(nomem, nostack));
    }
}

#[no_mangle]
pub extern "C" fn pthread_cond_signal(cond: *mut Cond) -> i32 {
    unsafe { cond_signal_internal(cond, false) }
    0
}

#[no_mangle]
pub extern "C" fn pthread_cond_broadcast(cond: *mut Cond) -> i32 {
    unsafe { cond_signal_internal(cond, true) }
    0
}

// -- TLS keys ----------------------------------------------------------

#[no_mangle]
pub extern "C" fn pthread_key_create(
    key: *mut u32,
    _dtor: Option<extern "C" fn(*mut core::ffi::c_void)>,
) -> i32 {
    unsafe {
        let k = NEXT_KEY;
        NEXT_KEY += 1;
        if NEXT_KEY >= MAX_TLS_KEYS {
            NEXT_KEY = 1;
        }
        *key = k as u32;
    }
    0
}

#[no_mangle]
pub extern "C" fn pthread_key_delete(_key: usize) -> i32 {
    0
}

#[no_mangle]
pub extern "C" fn pthread_getspecific(key: usize) -> *mut core::ffi::c_void {
    if key == 0 || key >= MAX_TLS_KEYS {
        return ptr::null_mut();
    }
    let slot = cur();
    unsafe { (*tcbs_mut().add(slot)).tls[key] as *mut core::ffi::c_void }
}

#[no_mangle]
pub extern "C" fn pthread_setspecific(
    key: usize,
    value: *const core::ffi::c_void,
) -> i32 {
    if key == 0 || key >= MAX_TLS_KEYS {
        return 22; // EINVAL
    }
    let slot = cur();
    unsafe { (*tcbs_mut().add(slot)).tls[key] = value as usize }
    0
}
