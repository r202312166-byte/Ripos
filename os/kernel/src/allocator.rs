//! First-fit free-list allocator for the kernel's Rust heap.
//!
//! Replaces the old bump allocator (which never freed).  Blocks form an
//! address-ordered chain (each block header carries `size`, `free`, `next`);
//! `next` always points at the address-adjacent block, so allocation walks
//! free blocks and `dealloc` finds its block by walking the chain.  Freeing
//! coalesces with the following free block(s).
//!
//! The layout mirrors the C first-fit heap in `libc.rs` (16-byte alignment,
//! 32-byte header) but adds support for arbitrary `Layout` alignment: the
//! returned pointer is aligned within the block, and the header of an
//! allocation is not necessarily at `ptr - 32`, so `dealloc` locates the
//! header by walking the chain instead of pointer arithmetic.

use core::alloc::{GlobalAlloc, Layout};
use core::ptr;
use core::sync::atomic::{AtomicBool, AtomicUsize, Ordering};

const BLOCK_HDR: usize = 32;
const BLOCK_ALIGN: usize = 16;

#[repr(C)]
struct Block {
    size: usize, // usable bytes (excluding header)
    free: bool,
    next: *mut Block,
}

fn align_up(v: usize, a: usize) -> usize {
    (v + a - 1) & !(a - 1)
}

/// First-fit allocation over the chain rooted at `start`.
/// Returns the user pointer, or null when no free block fits.
unsafe fn first_fit_alloc(start: usize, end: usize, size: usize, align: usize) -> *mut u8 {
    let mut b = start as *mut Block;
    while !b.is_null() {
        let baddr = b as usize;
        if baddr < start || baddr + BLOCK_HDR > end {
            break;
        }
        if (*b).free {
            let user = align_up(baddr + BLOCK_HDR, align);
            let alloc_end = user + size;
            let block_end = baddr + BLOCK_HDR + (*b).size;
            if alloc_end <= block_end {
                // Split the remainder off if it is worth keeping.  The split
                // point is 16-aligned so the remainder's header stays aligned.
                let split = align_up(alloc_end, BLOCK_ALIGN);
                let rem_size = block_end.saturating_sub(split);
                if rem_size >= BLOCK_HDR + 16 {
                    let rem = split as *mut Block;
                    (*rem).size = rem_size - BLOCK_HDR;
                    (*rem).free = true;
                    (*rem).next = (*b).next;
                    (*b).size = split - baddr - BLOCK_HDR;
                    (*b).next = rem;
                }
                // No split: `size`/`next` keep their old values; the unused
                // tail stays trapped inside this block until it is freed.
                (*b).free = false;
                return user as *mut u8;
            }
        }
        b = (*b).next;
    }
    ptr::null_mut()
}

/// Find the block that contains `user` and return its address, or null.
unsafe fn block_containing(start: usize, end: usize, user: usize) -> *mut Block {
    let mut b = start as *mut Block;
    while !b.is_null() {
        let baddr = b as usize;
        if baddr < start || baddr + BLOCK_HDR > end {
            break;
        }
        let block_end = baddr + BLOCK_HDR + (*b).size;
        if user >= baddr + BLOCK_HDR && user < block_end {
            return b;
        }
        b = (*b).next;
    }
    ptr::null_mut()
}

/// Free `user`'s block and coalesce with the following free block(s).
unsafe fn first_fit_free(start: usize, end: usize, user: usize) {
    let b = block_containing(start, end, user);
    if b.is_null() {
        return;
    }
    if (*b).free {
        // Double free: ignore.
        return;
    }
    (*b).free = true;
    let mut n = (*b).next;
    while !n.is_null() && (*n).free {
        (*b).size += BLOCK_HDR + (*n).size;
        (*b).next = (*n).next;
        n = (*b).next;
    }
}

/// Simple free-list allocator over a static region, guarded by a spinlock.
pub struct FreeListAllocator {
    heap_start: AtomicUsize,
    heap_end: AtomicUsize,
    lock: AtomicBool,
}

impl FreeListAllocator {
    pub const fn new() -> Self {
        Self {
            heap_start: AtomicUsize::new(0),
            heap_end: AtomicUsize::new(0),
            lock: AtomicBool::new(false),
        }
    }

    /// Initialize the heap region. Must be called once before any allocation.
    pub fn init(&self, start: usize, size: usize) {
        self.heap_start.store(start, Ordering::Relaxed);
        self.heap_end.store(start + size, Ordering::Relaxed);
        unsafe {
            let b = start as *mut Block;
            b.write(Block {
                size: size - BLOCK_HDR,
                free: true,
                next: ptr::null_mut(),
            });
        }
    }

    pub fn is_initialized(&self) -> bool {
        self.heap_end.load(Ordering::Relaxed) != 0
    }

    fn acquire(&self) {
        while self
            .lock
            .compare_exchange_weak(false, true, Ordering::Acquire, Ordering::Relaxed)
            .is_err()
        {
            core::hint::spin_loop();
        }
    }

    fn release(&self) {
        self.lock.store(false, Ordering::Release);
    }

    /// Bytes currently handed out (walk the free list and subtract).
    pub fn used_bytes(&self) -> usize {
        let start = self.heap_start.load(Ordering::Relaxed);
        let end = self.heap_end.load(Ordering::Relaxed);
        if end <= start {
            return 0;
        }
        unsafe {
            let mut free = 0usize;
            let mut b = start as *mut Block;
            while !b.is_null() {                let baddr = b as usize;
                if baddr < start || baddr + BLOCK_HDR > end {
                    break;
                }
                if (*b).free {
                    free += BLOCK_HDR + (*b).size;
                }
                b = (*b).next;
            }
            (end - start) - free
        }
    }

    pub fn heap_size(&self) -> usize {
        let end = self.heap_end.load(Ordering::Relaxed);
        let start = self.heap_start.load(Ordering::Relaxed);
        end - start
    }

    /// Boot-time self-test: allocation patterns, coalescing, alignment and a
    /// small stress run.  Prints progress to the serial console.
    pub unsafe fn selftest(&self) -> bool {
        let mut ok = true;
        let mut check = |cond: bool, what: &str| {
            crate::serial::write_str(if cond { "  ok: " } else { "  FAIL: " });
            crate::serial::write_str(what);
            crate::serial::write_str("\n");
            if !cond {
                ok = false;
            }
        };
        unsafe {
            // 1. basic alloc/free/realloc
            let a = self.alloc(Layout::from_size_align(8, 8).unwrap());
            let b = self.alloc(Layout::from_size_align(64, 8).unwrap());
            let c = self.alloc(Layout::from_size_align(128, 16).unwrap());
            check(!a.is_null() && !b.is_null() && !c.is_null(), "basic allocs");
            check(
                a as usize % 8 == 0 && b as usize % 8 == 0 && c as usize % 16 == 0,
                "basic alignment",
            );
            let used0 = self.used_bytes();
            check(used0 >= 8 + 64 + 128, "used_bytes tracks allocations");
            self.dealloc(b, Layout::from_size_align(64, 8).unwrap());
            let d = self.alloc(Layout::from_size_align(32, 8).unwrap());
            check(d == b, "freed block is reused (first-fit)");
            self.dealloc(d, Layout::from_size_align(32, 8).unwrap());
            self.dealloc(c, Layout::from_size_align(128, 16).unwrap());
            self.dealloc(a, Layout::from_size_align(8, 8).unwrap());

            // 2. coalescing: alloc three adjacent, free two neighbours, then
            //    allocate a block bigger than one -> must use the merged run.
            let x = self.alloc(Layout::from_size_align(256, 8).unwrap());
            let y = self.alloc(Layout::from_size_align(256, 8).unwrap());
            let z = self.alloc(Layout::from_size_align(256, 8).unwrap());
            check(!x.is_null() && !y.is_null() && !z.is_null(), "adjacent allocs");
            // Free z first, then y: y must coalesce with the free z run.
            self.dealloc(z, Layout::from_size_align(256, 8).unwrap());
            self.dealloc(y, Layout::from_size_align(256, 8).unwrap());
            let w = self.alloc(Layout::from_size_align(384, 8).unwrap());
            check(w == y, "coalescing reuses the merged run");
            self.dealloc(w, Layout::from_size_align(384, 8).unwrap());
            self.dealloc(x, Layout::from_size_align(256, 8).unwrap());

            // 3. large alignment (page-sized)
            let p = self.alloc(Layout::from_size_align(64, 4096).unwrap());
            check(!p.is_null() && p as usize % 4096 == 0, "4096-aligned alloc");
            self.dealloc(p, Layout::from_size_align(64, 4096).unwrap());

            // 4. stress: many small allocs, free every other
            let mut ptrs = [ptr::null_mut::<u8>(); 256];
            let mut n = 0usize;
            let mut all_ok = true;
            for i in 0..256usize {
                let size = (i * 37) % 1024 + 1;
                let l = Layout::from_size_align(size, 16).unwrap();
                let q = self.alloc(l);
                if q.is_null() || q as usize % 16 != 0 {
                    all_ok = false;
                    break;
                }
                q.write_bytes(i as u8, size);
                ptrs[i] = q;
                n += 1;
            }
            check(all_ok, "stress allocs (256 blocks)");
            for i in (0..n).step_by(2) {
                let size = (i * 37) % 1024 + 1;
                self.dealloc(ptrs[i], Layout::from_size_align(size, 16).unwrap());
            }
            for i in (1..n).step_by(2) {
                let size = (i * 37) % 1024 + 1;
                self.dealloc(ptrs[i], Layout::from_size_align(size, 16).unwrap());
            }

            // 5. after freeing everything, one big alloc must span the heap
            let big = self.alloc(Layout::from_size_align(8 * 1024 * 1024, 16).unwrap());
            check(!big.is_null(), "whole-heap alloc after coalescing");
            if !big.is_null() {
                self.dealloc(big, Layout::from_size_align(8 * 1024 * 1024, 16).unwrap());
            }
        }
        crate::serial::write_str(if ok {
            "allocator selftest: PASS\n"
        } else {
            "allocator selftest: FAIL\n"
        });
        ok
    }
}

/// The kernel's global allocator (used by `alloc::*`).
#[global_allocator]
pub static GLOBAL_ALLOCATOR: FreeListAllocator = FreeListAllocator::new();

/// Heap stats exported to the C shim for `kern.alloc_stats()`.
#[no_mangle]
pub unsafe extern "C" fn kern_alloc_used() -> usize {
    GLOBAL_ALLOCATOR.used_bytes()
}

#[no_mangle]
pub unsafe extern "C" fn kern_alloc_total() -> usize {
    GLOBAL_ALLOCATOR.heap_size()
}

/// Diagnostic: bytes allocated so far (via kern_alloc_used).
pub fn used_bytes_probe() -> usize {
    GLOBAL_ALLOCATOR.used_bytes()
}

unsafe impl GlobalAlloc for FreeListAllocator {
    unsafe fn alloc(&self, layout: Layout) -> *mut u8 {
        let size = layout.size().max(1);
        let align = layout.align().max(1);
        self.acquire();
        let start = self.heap_start.load(Ordering::Relaxed);
        let end = self.heap_end.load(Ordering::Relaxed);
        let result = if start != 0 {
            unsafe { first_fit_alloc(start, end, size, align) }
        } else {
            ptr::null_mut()
        };
        self.release();
        result
    }

    unsafe fn dealloc(&self, ptr: *mut u8, _layout: Layout) {
        if ptr.is_null() {
            return;
        }
        self.acquire();
        let start = self.heap_start.load(Ordering::Relaxed);
        let end = self.heap_end.load(Ordering::Relaxed);
        unsafe { first_fit_free(start, end, ptr as usize) };
        self.release();
    }

    unsafe fn realloc(&self, ptr: *mut u8, layout: Layout, new_size: usize) -> *mut u8 {
        if ptr.is_null() {
            return unsafe {
                self.alloc(Layout::from_size_align(new_size.max(1), layout.align()).unwrap())
            };
        }
        self.acquire();
        let start = self.heap_start.load(Ordering::Relaxed);
        let end = self.heap_end.load(Ordering::Relaxed);
        let room = if start != 0 {
            let b = unsafe { block_containing(start, end, ptr as usize) };
            if b.is_null() {
                0
            } else {
                let baddr = b as usize;
                let block_end = baddr + BLOCK_HDR + unsafe { (*b).size };
                block_end - ptr as usize
            }
        } else {
            0
        };
        self.release();
        if new_size <= room {
            return ptr;
        }
        let np = unsafe {
            self.alloc(Layout::from_size_align(new_size.max(1), layout.align()).unwrap())
        };
        if np.is_null() {
            return ptr::null_mut();
        }
        let copy = if room < new_size { room } else { new_size };
        unsafe { ptr::copy_nonoverlapping(ptr, np, copy) };
        unsafe { self.dealloc(ptr, layout) };
        np
    }
}
