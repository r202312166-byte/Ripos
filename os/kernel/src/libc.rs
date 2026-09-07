//! libc shim for the embedded CPython (LP64 musl-flavored ABI).
//!
//! The CPython objects are compiled with `zig cc -target x86_64-linux-musl`:
//! SysV calling convention, `long` = 8 bytes, `wchar_t` = 4 bytes, and musl
//! struct layouts (timespec = {i64,i64}, stat = 144 bytes, tm has
//! tm_gmtoff/tm_zone, timeval = {i64,i64}).  Every layout here must match
//! those headers.  No filesystem exists yet; serial console is fd 1/2,
//! /dev/urandom is a TSC-based PRNG on fd 100.

use core::ffi::{c_char, c_int, c_void};
use core::ptr;

pub const ENOENT: i32 = 2;
pub const EBADF: i32 = 9;
pub const EINVAL: i32 = 22;
pub const ENOTTY: i32 = 25;
pub const ENOSYS: i32 = 38;
pub const EPERM: i32 = 1;
pub const ECHILD: i32 = 10;
pub const EAGAIN: i32 = 11;
pub const EACCES: i32 = 13;
pub const ESRCH: i32 = 3;
pub const ENOMEM: i32 = 12;
pub const EINTR: i32 = 4;

// ---------------------------------------------------------------------------
// errno (musl reads it via __errno_location())
// ---------------------------------------------------------------------------

static mut ERRNO: c_int = 0;

#[no_mangle]
pub extern "C" fn __errno_location() -> *mut c_int {
    unsafe { &mut ERRNO }
}

#[no_mangle]
pub extern "C" fn _errno() -> *mut c_int {
    unsafe { &mut ERRNO }
}

pub fn set_errno(e: c_int) {
    unsafe {
        ERRNO = e;
    }
}

// ---------------------------------------------------------------------------
// first-fit heap allocator (C malloc) -- unchanged
// ---------------------------------------------------------------------------

#[repr(C)]
struct Block {
    size: usize,
    free: bool,
    next: *mut Block,
    prev: *mut Block,
}

const BLOCK_HDR: usize = 32;
const BLOCK_ALIGN: usize = 16;
static mut HEAP_START: *mut Block = ptr::null_mut();
static mut HEAP_END: usize = 0;
static mut HEAP_LOCK: bool = false;

pub fn heap_start() -> usize {
    unsafe { HEAP_START as usize }
}

pub fn heap_init(start: usize, size: usize) {
    unsafe {
        HEAP_START = start as *mut Block;
        HEAP_END = start + size;
        let b = &mut *HEAP_START;
        b.size = size - BLOCK_HDR;
        b.free = true;
        b.next = ptr::null_mut();
        b.prev = ptr::null_mut();
    }
}

fn heap_lock() {
    unsafe {
        while HEAP_LOCK {
            core::hint::spin_loop();
        }
        HEAP_LOCK = true;
    }
}

fn heap_unlock() {
    unsafe {
        HEAP_LOCK = false;
    }
}

unsafe fn malloc_impl(size: usize) -> *mut c_void {
    let size = size.max(1);
    let need = (size + BLOCK_HDR).max(BLOCK_ALIGN).div_ceil(BLOCK_ALIGN) * BLOCK_ALIGN;
    heap_lock();
    let mut b = HEAP_START;
    let result;
    loop {
        if b.is_null() {
            result = ptr::null_mut();
            break;
        }
        if (*b).free && (*b).size >= need - BLOCK_HDR {
            if (*b).size >= need + BLOCK_HDR + 16 {
                let rem = (b as usize + need) as *mut Block;
                (*rem).size = (*b).size - need;
                (*rem).free = true;
                (*rem).next = (*b).next;
                (*rem).prev = b;
                if !(*rem).next.is_null() {
                    (*(*rem).next).prev = rem;
                }
                (*b).size = need - BLOCK_HDR;
                (*b).next = rem;
            }
            (*b).free = false;
            result = (b as usize + BLOCK_HDR) as *mut c_void;
            break;
        }
        b = (*b).next;
    }
    heap_unlock();
    result
}

unsafe fn free_impl(ptr: *mut c_void) {
    if ptr.is_null() {
        return;
    }
    let b = (ptr as usize - BLOCK_HDR) as *mut Block;
    heap_lock();
    (*b).free = true;
    // forward merge: absorb any following free blocks
    let mut n = (*b).next;
    while !n.is_null() && (*n).free {
        (*b).size += BLOCK_HDR + (*n).size;
        (*b).next = (*n).next;
        if !(*b).next.is_null() {
            (*(*b).next).prev = b;
        }
        n = (*b).next;
    }
    // backward merge: if the previous block is free, absorb us into it
    // (blocks freed out of order leave adjacent free blocks that forward
    // coalescing alone can never join; that fragmentation is what made
    // large image buffers fail after a media cycle)
    let p = (*b).prev;
    if !p.is_null() && (*p).free {
        (*p).size += BLOCK_HDR + (*b).size;
        (*p).next = (*b).next;
        if !(*p).next.is_null() {
            (*(*p).next).prev = p;
        }
    }
    heap_unlock();
}

unsafe fn realloc_impl(ptr: *mut c_void, size: usize) -> *mut c_void {
    if ptr.is_null() {
        return malloc_impl(size);
    }
    let b = (ptr as usize - BLOCK_HDR) as *mut Block;
    if (*b).size >= size {
        return ptr;
    }
    let newp = malloc_impl(size);
    if newp.is_null() {
        return ptr::null_mut();
    }
    ptr::copy_nonoverlapping(ptr, newp, (*b).size.min(size));
    free_impl(ptr);
    newp
}

/// Walk the C-heap free list: returns (total free bytes, largest free
/// block).  Exposed to Python as kern.c_heap_stats() so app/OS developers
/// can see heap pressure (fragmentation shows as a largest block far below
/// the total free).
#[no_mangle]
pub extern "C" fn kern_c_heap_stats(free_total: *mut u64, largest: *mut u64) -> i32 {
    unsafe {
        let mut total: u64 = 0;
        let mut maxb: u64 = 0;
        let mut b = HEAP_START;
        while !b.is_null() {
            if (*b).free {
                total += (*b).size as u64;
                if (*b).size as u64 > maxb {
                    maxb = (*b).size as u64;
                }
            }
            b = (*b).next;
        }
        if !free_total.is_null() {
            *free_total = total;
        }
        if !largest.is_null() {
            *largest = maxb;
        }
    }
    0
}

#[no_mangle]
pub extern "C" fn malloc(size: usize) -> *mut c_void {
    unsafe { malloc_impl(size) }
}

static mut MALLOC_LOG: u32 = 0;

#[no_mangle]
pub extern "C" fn calloc(nmemb: usize, size: usize) -> *mut c_void {
    let total = nmemb.saturating_mul(size);
    let p = unsafe { malloc_impl(total) };
    if !p.is_null() {
        unsafe {
            ptr::write_bytes(p, 0, total);
        }
    }
    unsafe {
        if MALLOC_LOG < 24 {
            MALLOC_LOG += 1;
            crate::serial::write_str("calloc(");
            crate::serial::write_u64(total as u64);
            crate::serial::write_str(")->");
            crate::serial::write_u64(p as u64);
            crate::serial::write_str("\n");
        }
    }
    p
}

#[no_mangle]
pub extern "C" fn realloc(ptr: *mut c_void, size: usize) -> *mut c_void {
    unsafe { realloc_impl(ptr, size) }
}

#[no_mangle]
pub extern "C" fn free(ptr: *mut c_void) {
    unsafe { free_impl(ptr) }
}

// ---------------------------------------------------------------------------
// memory / string
// ---------------------------------------------------------------------------

// NOTE: these are implemented with explicit byte loops, never
// ptr::copy_nonoverlapping/write_bytes: at -O0 those intrinsics lower to
// *calls* to the very symbol we are defining, causing infinite recursion.

#[no_mangle]
pub unsafe extern "C" fn memcpy(dst: *mut c_void, src: *const c_void, n: usize) -> *mut c_void {
    let d = dst as *mut u8;
    let s = src as *const u8;
    let mut i = 0usize;
    while i < n {
        unsafe { *d.add(i) = *s.add(i) };
        i += 1;
    }
    dst
}

#[no_mangle]
pub unsafe extern "C" fn memmove(dst: *mut c_void, src: *const c_void, n: usize) -> *mut c_void {
    let d = dst as *mut u8;
    let s = src as *const u8;
    if (s as usize) < (d as usize) {
        // copy backwards
        let mut i = n;
        while i > 0 {
            i -= 1;
            unsafe { *d.add(i) = *s.add(i) };
        }
    } else {
        let mut i = 0usize;
        while i < n {
            unsafe { *d.add(i) = *s.add(i) };
            i += 1;
        }
    }
    dst
}

#[no_mangle]
pub unsafe extern "C" fn explicit_bzero(s: *mut c_void, n: usize) {
    // Volatile zeroing so the compiler cannot elide it (used by the HACL*
    // hashlib code for wiping sensitive state).
    let p = s as *mut u8;
    for i in 0..n {
        unsafe {
            core::ptr::write_volatile(p.add(i), 0u8);
        }
    }
}

#[no_mangle]
pub unsafe extern "C" fn memset(dst: *mut c_void, c: c_int, n: usize) -> *mut c_void {
    let d = dst as *mut u8;
    let mut i = 0usize;
    while i < n {
        unsafe { *d.add(i) = c as u8 };
        i += 1;
    }
    dst
}

#[no_mangle]
pub unsafe extern "C" fn bcmp(a: *const c_void, b: *const c_void, n: usize) -> c_int {
    // BSD bcmp: 0 iff the two buffers are equal (order irrelevant).
    unsafe { memcmp(a, b, n) }
}

#[no_mangle]
pub unsafe extern "C" fn memcmp(a: *const c_void, b: *const c_void, n: usize) -> c_int {
    let a = a as *const u8;
    let b = b as *const u8;
    for i in 0..n {
        let x = unsafe { *a.add(i) };
        let y = unsafe { *b.add(i) };
        if x != y {
            return x as c_int - y as c_int;
        }
    }
    0
}

#[no_mangle]
pub unsafe extern "C" fn memchr(s: *const c_void, c: c_int, n: usize) -> *mut c_void {
    let s = s as *const u8;
    for i in 0..n {
        if unsafe { *s.add(i) } == c as u8 {
            return unsafe { s.add(i) } as *mut c_void;
        }
    }
    ptr::null_mut()
}

#[no_mangle]
pub unsafe extern "C" fn memrchr(s: *const c_void, c: c_int, n: usize) -> *mut c_void {
    let s = s as *const u8;
    let mut i = n;
    while i > 0 {
        i -= 1;
        if unsafe { *s.add(i) } == c as u8 {
            return unsafe { s.add(i) } as *mut c_void;
        }
    }
    ptr::null_mut()
}

fn cstr_len(s: *const c_char) -> usize {
    let mut n = 0;
    while unsafe { *s.add(n) } != 0 {
        n += 1;
    }
    n
}

#[no_mangle]
pub unsafe extern "C" fn strlen(s: *const c_char) -> usize {
    cstr_len(s)
}

#[no_mangle]
pub unsafe extern "C" fn strcat(dst: *mut c_char, src: *const c_char) -> *mut c_char {
    let mut i = 0;
    while unsafe { *dst.add(i) } != 0 {
        i += 1;
    }
    let mut j = 0;
    loop {
        let c = unsafe { *src.add(j) };
        unsafe { *dst.add(i) = c };
        if c == 0 {
            break;
        }
        i += 1;
        j += 1;
    }
    dst
}

#[no_mangle]
pub unsafe extern "C" fn strncat(dst: *mut c_char, src: *const c_char, n: usize) -> *mut c_char {
    let mut i = 0;
    while unsafe { *dst.add(i) } != 0 {
        i += 1;
    }
    let mut j = 0;
    while j < n && unsafe { *src.add(j) } != 0 {
        unsafe { *dst.add(i) = *src.add(j) };
        i += 1;
        j += 1;
    }
    unsafe { *dst.add(i) = 0 };
    dst
}

#[no_mangle]
pub unsafe extern "C" fn strcmp(a: *const c_char, b: *const c_char) -> c_int {
    let mut i = 0;
    loop {
        let x = unsafe { *a.add(i) } as u8;
        let y = unsafe { *b.add(i) } as u8;
        if x != y {
            return x as c_int - y as c_int;
        }
        if x == 0 {
            return 0;
        }
        i += 1;
    }
}

#[no_mangle]
pub unsafe extern "C" fn strncmp(a: *const c_char, b: *const c_char, n: usize) -> c_int {
    for i in 0..n {
        let x = unsafe { *a.add(i) } as u8;
        let y = unsafe { *b.add(i) } as u8;
        if x != y {
            return x as c_int - y as c_int;
        }
        if x == 0 {
            return 0;
        }
    }
    0
}

#[no_mangle]
pub unsafe extern "C" fn strcpy(dst: *mut c_char, src: *const c_char) -> *mut c_char {
    let mut i = 0;
    loop {
        let c = unsafe { *src.add(i) };
        unsafe { *dst.add(i) = c };
        if c == 0 {
            break;
        }
        i += 1;
    }
    dst
}

#[no_mangle]
pub unsafe extern "C" fn strncpy(dst: *mut c_char, src: *const c_char, n: usize) -> *mut c_char {
    let mut i = 0;
    while i < n {
        let c = unsafe { *src.add(i) };
        unsafe { *dst.add(i) = c };
        if c == 0 {
            break;
        }
        i += 1;
    }
    dst
}

#[no_mangle]
pub unsafe extern "C" fn strchr(s: *const c_char, c: c_int) -> *mut c_char {
    let mut i = 0;
    loop {
        let x = unsafe { *s.add(i) };
        if x as c_int == c {
            return unsafe { s.add(i) } as *mut c_char;
        }
        if x == 0 {
            return ptr::null_mut();
        }
        i += 1;
    }
}

#[no_mangle]
pub unsafe extern "C" fn strrchr(s: *const c_char, c: c_int) -> *mut c_char {
    let mut i = cstr_len(s);
    loop {
        let x = unsafe { *s.add(i) };
        if x as c_int == c {
            return unsafe { s.add(i) } as *mut c_char;
        }
        if i == 0 {
            return ptr::null_mut();
        }
        i -= 1;
    }
}

#[no_mangle]
pub unsafe extern "C" fn strcspn(s: *const c_char, reject: *const c_char) -> usize {
    let mut i = 0;
    loop {
        let x = unsafe { *s.add(i) } as u8;
        if x == 0 {
            return i;
        }
        let mut j = 0;
        loop {
            let r = unsafe { *reject.add(j) } as u8;
            if r == 0 {
                break;
            }
            if r == x {
                return i;
            }
            j += 1;
        }
        i += 1;
    }
}

#[no_mangle]
pub unsafe extern "C" fn strpbrk(s: *const c_char, accept: *const c_char) -> *mut c_char {
    let mut i = 0;
    loop {
        let x = unsafe { *s.add(i) } as u8;
        if x == 0 {
            return ptr::null_mut();
        }
        let mut j = 0;
        loop {
            let a = unsafe { *accept.add(j) } as u8;
            if a == 0 {
                break;
            }
            if a == x {
                return unsafe { s.add(i) } as *mut c_char;
            }
            j += 1;
        }
        i += 1;
    }
}

#[no_mangle]
pub unsafe extern "C" fn strstr(hay: *const c_char, needle: *const c_char) -> *mut c_char {
    let nlen = cstr_len(needle);
    if nlen == 0 {
        return hay as *mut c_char;
    }
    let mut i = 0;
    loop {
        let x = unsafe { *hay.add(i) } as u8;
        if x == 0 {
            return ptr::null_mut();
        }
        // Compare needle at i.
        let mut ok = true;
        for k in 0..nlen {
            if unsafe { *hay.add(i + k) } as u8 != *needle.add(k) as u8 {
                ok = false;
                break;
            }
        }
        if ok {
            return unsafe { hay.add(i) } as *mut c_char;
        }
        i += 1;
    }
}

#[no_mangle]
pub unsafe extern "C" fn strtok_r(
    s: *mut c_char,
    delim: *const c_char,
    saveptr: *mut *mut c_char,
) -> *mut c_char {
    let start = if !s.is_null() { s } else { unsafe { *saveptr } };
    if start.is_null() {
        return ptr::null_mut();
    }
    let mut i = 0usize;
    let is_delim = |c: u8| -> bool {
        let mut j = 0usize;
        loop {
            let d = unsafe { *delim.add(j) } as u8;
            if d == 0 {
                return false;
            }
            if d == c {
                return true;
            }
            j += 1;
        }
    };
    while unsafe { *start.add(i) } != 0 && is_delim(unsafe { *start.add(i) } as u8) {
        i += 1;
    }
    if unsafe { *start.add(i) } == 0 {
        unsafe { *saveptr = ptr::null_mut() };
        return ptr::null_mut();
    }
    let tok = unsafe { start.add(i) };
    loop {
        let c = unsafe { *tok.add(i) } as u8;
        if c == 0 {
            unsafe { *saveptr = ptr::null_mut() };
            return tok as *mut c_char;
        }
        if is_delim(c) {
            unsafe {
                *tok.add(i) = 0;
                *saveptr = tok.add(i + 1);
            }
            return tok as *mut c_char;
        }
        i += 1;
    }
}

fn parse_long(s: *const c_char, endptr: *mut *mut c_char, base: c_int) -> (i64, usize) {
    let mut i = 0;
    let mut neg = false;
    let mut c = unsafe { *s.add(i) } as u8;
    while c == b' ' || c == b'\t' {
        i += 1;
        c = unsafe { *s.add(i) } as u8;
    }
    if c == b'+' || c == b'-' {
        neg = c == b'-';
        i += 1;
        c = unsafe { *s.add(i) } as u8;
    }
    let mut base = base as u64;
    if base == 0 {
        if c == b'0' {
            let c2 = unsafe { *s.add(i + 1) } as u8;
            if c2 == b'x' || c2 == b'X' {
                base = 16;
            } else {
                base = 8;
            }
        } else {
            base = 10;
        }
    }
    if (base == 16) && c == b'0' {
        let c2 = unsafe { *s.add(i + 1) } as u8;
        if c2 == b'x' || c2 == b'X' {
            i += 2;
        }
    }
    let mut val: u64 = 0;
    let mut any = false;
    loop {
        let c = unsafe { *s.add(i) } as u8;
        let d = match c {
            b'0'..=b'9' => (c - b'0') as u64,
            b'a'..=b'z' => (c - b'a' + 10) as u64,
            b'A'..=b'Z' => (c - b'A' + 10) as u64,
            _ => break,
        };
        if d >= base {
            break;
        }
        val = val.wrapping_mul(base).wrapping_add(d);
        any = true;
        i += 1;
    }
    if !any {
        i = 0;
    }
    if !endptr.is_null() {
        unsafe {
            *endptr = (s as usize + i) as *mut c_char;
        }
    }
    let v = if neg { (val as i64).wrapping_neg() } else { val as i64 };
    (v, i)
}

/// musl strtol returns `long` = i64.
#[no_mangle]
pub unsafe extern "C" fn strtol(
    s: *const c_char,
    endptr: *mut *mut c_char,
    base: c_int,
) -> i64 {
    parse_long(s, endptr, base).0
}

/// musl strtoul returns `unsigned long` = u64.
#[no_mangle]
pub unsafe extern "C" fn strtoul(
    s: *const c_char,
    endptr: *mut *mut c_char,
    base: c_int,
) -> u64 {
    parse_long(s, endptr, base).0 as u64
}

#[no_mangle]
pub unsafe extern "C" fn qsort(
    base: *mut c_void,
    nmemb: usize,
    size: usize,
    compar: Option<extern "C" fn(*const c_void, *const c_void) -> c_int>,
) {
    if nmemb < 2 || size == 0 {
        return;
    }
    let b = base as *mut u8;
    extern "C" fn cmp_zero(_: *const c_void, _: *const c_void) -> c_int {
        0
    }
    let cmp = compar.unwrap_or(cmp_zero);
    for i in 1..nmemb {
        let mut j = i;
        while j > 0 {
            let a = unsafe { b.add((j - 1) * size) };
            let c = unsafe { b.add(j * size) };
            if cmp(a as *const c_void, c as *const c_void) > 0 {
                for k in 0..size {
                    unsafe {
                        let t = *a.add(k);
                        *a.add(k) = *c.add(k);
                        *c.add(k) = t;
                    }
                }
                j -= 1;
            } else {
                break;
            }
        }
    }
}

#[no_mangle]
pub unsafe extern "C" fn tolower(c: c_int) -> c_int {
    if c >= b'A' as c_int && c <= b'Z' as c_int {
        c + (b'a' - b'A') as c_int
    } else {
        c
    }
}

#[no_mangle]
pub unsafe extern "C" fn toupper(c: c_int) -> c_int {
    if c >= b'a' as c_int && c <= b'z' as c_int {
        c - (b'a' - b'A') as c_int
    } else {
        c
    }
}

#[no_mangle]
pub unsafe extern "C" fn isalnum(c: c_int) -> c_int {
    ((c >= b'a' as c_int && c <= b'z' as c_int)
        || (c >= b'A' as c_int && c <= b'Z' as c_int)
        || (c >= b'0' as c_int && c <= b'9' as c_int)) as c_int
}

#[no_mangle]
pub unsafe extern "C" fn isxdigit(c: c_int) -> c_int {
    ((c >= b'0' as c_int && c <= b'9' as c_int)
        || (c >= b'a' as c_int && c <= b'f' as c_int)
        || (c >= b'A' as c_int && c <= b'F' as c_int)) as c_int
}

#[no_mangle]
pub unsafe extern "C" fn __popcountdi2(x: u64) -> c_int {
    x.count_ones() as c_int
}

/// MS-ABI stack probe (kept for compatibility; no-op on this build).
#[unsafe(naked)]
#[no_mangle]
pub unsafe extern "C" fn ___chkstk_ms() -> usize {
    core::arch::naked_asm!("ret")
}

// ---------------------------------------------------------------------------
// wide strings (musl wchar_t = 4 bytes)
// ---------------------------------------------------------------------------

fn wstr_len(s: *const u32) -> usize {
    let mut n = 0;
    while unsafe { *s.add(n) } != 0 {
        n += 1;
    }
    n
}

#[no_mangle]
pub unsafe extern "C" fn wcslen(s: *const u32) -> usize {
    wstr_len(s)
}

#[no_mangle]
pub unsafe extern "C" fn wcscmp(a: *const u32, b: *const u32) -> c_int {
    let mut i = 0;
    loop {
        let x = unsafe { *a.add(i) };
        let y = unsafe { *b.add(i) };
        if x != y {
            return x as c_int - y as c_int;
        }
        if x == 0 {
            return 0;
        }
        i += 1;
    }
}

#[no_mangle]
pub unsafe extern "C" fn wcsncmp(a: *const u32, b: *const u32, n: usize) -> c_int {
    for i in 0..n {
        let x = unsafe { *a.add(i) };
        let y = unsafe { *b.add(i) };
        if x != y {
            return x as c_int - y as c_int;
        }
        if x == 0 {
            return 0;
        }
    }
    0
}

#[no_mangle]
pub unsafe extern "C" fn wcscpy(dst: *mut u32, src: *const u32) -> *mut u32 {
    let mut i = 0;
    loop {
        let c = unsafe { *src.add(i) };
        unsafe { *dst.add(i) = c };
        if c == 0 {
            break;
        }
        i += 1;
    }
    dst
}

#[no_mangle]
pub unsafe extern "C" fn wcsncpy(dst: *mut u32, src: *const u32, n: usize) -> *mut u32 {
    let mut i = 0;
    while i < n {
        let c = unsafe { *src.add(i) };
        unsafe { *dst.add(i) = c };
        if c == 0 {
            break;
        }
        i += 1;
    }
    dst
}

#[no_mangle]
pub unsafe extern "C" fn wcschr(s: *const u32, c: u32) -> *mut u32 {
    let mut i = 0;
    loop {
        let x = unsafe { *s.add(i) };
        if x == c {
            return unsafe { s.add(i) } as *mut u32;
        }
        if x == 0 {
            return ptr::null_mut();
        }
        i += 1;
    }
}

#[no_mangle]
pub unsafe extern "C" fn wcsrchr(s: *const u32, c: u32) -> *mut u32 {
    let mut i = wstr_len(s);
    loop {
        let x = unsafe { *s.add(i) };
        if x == c {
            return unsafe { s.add(i) } as *mut u32;
        }
        if i == 0 {
            return ptr::null_mut();
        }
        i -= 1;
    }
}

#[no_mangle]
pub unsafe extern "C" fn wcscoll(a: *const u32, b: *const u32) -> c_int {
    wcscmp(a, b)
}

#[no_mangle]
pub unsafe extern "C" fn wcsxfrm(dst: *mut u32, src: *const u32, n: usize) -> usize {
    let len = wstr_len(src);
    if n > 0 {
        wcsncpy(dst, src, n - 1);
        unsafe { *dst.add(n - 1) = 0 };
    }
    len
}

#[no_mangle]
pub unsafe extern "C" fn wcstok(
    s: *mut u32,
    delim: *const u32,
    saveptr: *mut *mut u32,
) -> *mut u32 {
    let start = if !s.is_null() { s } else { unsafe { *saveptr } };
    if start.is_null() {
        return ptr::null_mut();
    }
    let mut i = 0;
    let is_delim = |c: u32| -> bool {
        let mut j = 0;
        loop {
            let d = unsafe { *delim.add(j) };
            if d == 0 {
                return false;
            }
            if d == c {
                return true;
            }
            j += 1;
        }
    };
    while unsafe { *start.add(i) } != 0 && is_delim(unsafe { *start.add(i) }) {
        i += 1;
    }
    if unsafe { *start.add(i) } == 0 {
        unsafe { *saveptr = ptr::null_mut() };
        return ptr::null_mut();
    }
    let tok = unsafe { start.add(i) };
    loop {
        let c = unsafe { *tok.add(i) };
        if c == 0 {
            unsafe { *saveptr = ptr::null_mut() };
            return tok as *mut u32;
        }
        if is_delim(c) {
            unsafe {
                *tok.add(i) = 0;
                *saveptr = tok.add(i + 1);
            }
            return tok as *mut u32;
        }
        i += 1;
    }
}

#[no_mangle]
pub unsafe extern "C" fn wcstol(s: *const u32, endptr: *mut *mut u32, base: c_int) -> i64 {
    let mut i = 0;
    let mut neg = false;
    let mut c = unsafe { *s.add(i) };
    while c == b' ' as u32 || c == b'\t' as u32 {
        i += 1;
        c = unsafe { *s.add(i) };
    }
    if c == b'+' as u32 || c == b'-' as u32 {
        neg = c == b'-' as u32;
        i += 1;
    }
    let mut base = if base == 0 { 10 } else { base as u64 };
    let mut val: u64 = 0;
    let mut any = false;
    loop {
        let c = unsafe { *s.add(i) } as u8;
        let d = match c {
            b'0'..=b'9' => (c - b'0') as u64,
            b'a'..=b'z' => (c - b'a' + 10) as u64,
            b'A'..=b'Z' => (c - b'A' + 10) as u64,
            _ => break,
        };
        if d >= base {
            break;
        }
        val = val.wrapping_mul(base).wrapping_add(d);
        any = true;
        i += 1;
    }
    if !any {
        i = 0;
    }
    if !endptr.is_null() {
        unsafe { *endptr = (s as usize + i) as *mut u32 };
    }
    (if neg { (val as i64).wrapping_neg() } else { val as i64 })
}

#[no_mangle]
pub unsafe extern "C" fn wmemchr(s: *const u32, c: u32, n: usize) -> *mut u32 {
    for i in 0..n {
        if unsafe { *s.add(i) } == c {
            return unsafe { s.add(i) } as *mut u32;
        }
    }
    ptr::null_mut()
}

#[no_mangle]
pub unsafe extern "C" fn wmemcmp(a: *const u32, b: *const u32, n: usize) -> c_int {
    for i in 0..n {
        let x = unsafe { *a.add(i) };
        let y = unsafe { *b.add(i) };
        if x != y {
            return x as c_int - y as c_int;
        }
    }
    0
}

#[no_mangle]
pub unsafe extern "C" fn wcstombs(dst: *mut c_char, src: *const u32, n: usize) -> usize {
    if dst.is_null() {
        return 0;
    }
    let mut i = 0;
    while i < n && unsafe { *src.add(i) } != 0 {
        let c = unsafe { *src.add(i) };
        if c < 0x80 {
            unsafe { *dst.add(i) = c as c_char };
        } else {
            let ch = core::char::from_u32(c).unwrap_or('\u{FFFD}');
            let mut buf = [0u8; 4];
            let enc = ch.encode_utf8(&mut buf);
            if i + enc.len() > n {
                break;
            }
            for (k, b) in enc.bytes().enumerate() {
                unsafe { *dst.add(i + k) = b as c_char };
            }
            i += enc.len();
            continue;
        }
        i += 1;
    }
    if i < n {
        unsafe { *dst.add(i) = 0 };
    }
    i
}

#[no_mangle]
pub unsafe extern "C" fn mbrtowc(
    pwc: *mut u32,
    s: *const c_char,
    n: usize,
    _ps: *mut c_void,
) -> usize {
    if s.is_null() {
        return 0;
    }
    if n == 0 {
        return 0;
    }
    let b = unsafe { *s } as u8;
    if b < 0x80 {
        if !pwc.is_null() {
            unsafe { *pwc = b as u32 };
        }
        return 1;
    }
    usize::MAX // error
}

#[no_mangle]
pub unsafe extern "C" fn mbstowcs(dst: *mut u32, s: *const c_char, n: usize) -> usize {
    if dst.is_null() {
        return 0;
    }
    let mut i = 0;
    while i < n && unsafe { *s.add(i) } != 0 {
        let b = unsafe { *s.add(i) } as u8;
        if b < 0x80 {
            unsafe { *dst.add(i) = b as u32 };
        } else {
            return usize::MAX;
        }
        i += 1;
    }
    if i < n {
        unsafe { *dst.add(i) = 0 };
    }
    i
}

// ---------------------------------------------------------------------------
// locale
// ---------------------------------------------------------------------------

#[repr(C)]
struct Lconv {
    decimal_point: *mut c_char,
    thousands_sep: *mut c_char,
    grouping: *mut c_char,
    int_curr_symbol: *mut c_char,
    currency_symbol: *mut c_char,
    mon_decimal_point: *mut c_char,
    mon_thousands_sep: *mut c_char,
    mon_grouping: *mut c_char,
    positive_sign: *mut c_char,
    negative_sign: *mut c_char,
    int_frac_digits: c_char,
    frac_digits: c_char,
    p_cs_precedes: c_char,
    p_sep_by_space: c_char,
    n_cs_precedes: c_char,
    n_sep_by_space: c_char,
    p_sign_posn: c_char,
    n_sign_posn: c_char,
    int_p_cs_precedes: c_char,
    int_p_sep_by_space: c_char,
    int_n_cs_precedes: c_char,
    int_n_sep_by_space: c_char,
    int_p_sign_posn: c_char,
    int_n_sign_posn: c_char,
}

static mut DOT: u8 = b'.';
static mut EMPTY: u8 = 0;
static mut GROUP: u8 = 0;
static mut LC: Lconv = Lconv {
    decimal_point: ptr::null_mut(),
    thousands_sep: ptr::null_mut(),
    grouping: ptr::null_mut(),
    int_curr_symbol: ptr::null_mut(),
    currency_symbol: ptr::null_mut(),
    mon_decimal_point: ptr::null_mut(),
    mon_thousands_sep: ptr::null_mut(),
    mon_grouping: ptr::null_mut(),
    positive_sign: ptr::null_mut(),
    negative_sign: ptr::null_mut(),
    int_frac_digits: 0,
    frac_digits: 0,
    p_cs_precedes: 0,
    p_sep_by_space: 0,
    n_cs_precedes: 0,
    n_sep_by_space: 0,
    p_sign_posn: 0,
    n_sign_posn: 0,
    int_p_cs_precedes: 0,
    int_p_sep_by_space: 0,
    int_n_cs_precedes: 0,
    int_n_sep_by_space: 0,
    int_p_sign_posn: 0,
    int_n_sign_posn: 0,
};

#[no_mangle]
pub unsafe extern "C" fn localeconv() -> *mut Lconv {
    unsafe {
        LC.decimal_point = &mut DOT as *mut u8 as *mut c_char;
        LC.thousands_sep = &mut EMPTY as *mut u8 as *mut c_char;
        LC.grouping = &mut GROUP as *mut u8 as *mut c_char;
        &mut LC
    }
}

static mut LOCALE_BUF: [c_char; 8] = [b'C' as c_char, 0, 0, 0, 0, 0, 0, 0];

#[no_mangle]
pub unsafe extern "C" fn setlocale(_category: c_int, locale: *const c_char) -> *mut c_char {
    if !locale.is_null() {
        let s = locale as *const c_char;
        if unsafe { *s } == 0 {
            return LOCALE_BUF.as_mut_ptr();
        }
    }
    LOCALE_BUF.as_mut_ptr()
}

// ---------------------------------------------------------------------------
// environment / process
// ---------------------------------------------------------------------------

#[no_mangle]
pub unsafe extern "C" fn getenv(_name: *const c_char) -> *mut c_char {
    ptr::null_mut()
}

#[no_mangle]
pub unsafe extern "C" fn setenv(_n: *const c_char, _v: *const c_char, _o: c_int) -> c_int {
    0
}

#[no_mangle]
pub unsafe extern "C" fn unsetenv(_n: *const c_char) -> c_int {
    0
}

static mut ENVIRON_END: [*mut c_char; 1] = [ptr::null_mut()];

/// musl exposes `char **environ`.
#[no_mangle]
pub static mut environ: *mut *mut c_char = &raw mut ENVIRON_END as *mut *mut c_char;

#[no_mangle]
pub unsafe extern "C" fn getpid() -> c_int {
    1
}

#[no_mangle]
pub unsafe extern "C" fn getppid() -> c_int {
    0
}

#[no_mangle]
pub unsafe extern "C" fn getuid() -> c_int {
    0
}

#[no_mangle]
pub unsafe extern "C" fn geteuid() -> c_int {
    0
}

#[no_mangle]
pub unsafe extern "C" fn getgid() -> c_int {
    0
}

#[no_mangle]
pub unsafe extern "C" fn getegid() -> c_int {
    0
}

#[no_mangle]
pub unsafe extern "C" fn getgroups(_n: c_int, _list: *mut u32) -> c_int {
    0
}

#[no_mangle]
pub unsafe extern "C" fn setgroups(_n: usize, _list: *const u32) -> c_int {
    set_errno(EPERM);
    -1
}

#[no_mangle]
pub unsafe extern "C" fn initgroups(_u: *const c_char, _g: u32) -> c_int {
    set_errno(EPERM);
    -1
}

#[no_mangle]
pub unsafe extern "C" fn getgrouplist(
    _u: *const c_char,
    _g: u32,
    _groups: *mut u32,
    _ngroups: *mut c_int,
) -> c_int {
    -1
}

#[no_mangle]
pub unsafe extern "C" fn getpgid(_pid: c_int) -> c_int {
    0
}

#[no_mangle]
pub unsafe extern "C" fn getpgrp() -> c_int {
    0
}

#[no_mangle]
pub unsafe extern "C" fn setpgid(_a: c_int, _b: c_int) -> c_int {
    0
}

#[no_mangle]
pub unsafe extern "C" fn setpgrp() -> c_int {
    0
}

#[no_mangle]
pub unsafe extern "C" fn getsid(_pid: c_int) -> c_int {
    0
}

#[no_mangle]
pub unsafe extern "C" fn setsid() -> c_int {
    0
}

#[no_mangle]
pub unsafe extern "C" fn getlogin() -> *mut c_char {
    ptr::null_mut()
}

#[no_mangle]
pub unsafe extern "C" fn getpriority(_which: c_int, _who: u32) -> c_int {
    0
}

#[no_mangle]
pub unsafe extern "C" fn setpriority(_which: c_int, _who: u32, _prio: c_int) -> c_int {
    0
}

#[no_mangle]
pub unsafe extern "C" fn nice(_inc: c_int) -> c_int {
    0
}

#[repr(C)]
pub struct Rlimit {
    pub rlim_cur: u64,
    pub rlim_max: u64,
}

#[no_mangle]
pub unsafe extern "C" fn getrlimit(_res: c_int, _rl: *mut Rlimit) -> c_int {
    set_errno(ENOSYS);
    -1
}

#[no_mangle]
pub unsafe extern "C" fn setrlimit(_res: c_int, _rl: *const Rlimit) -> c_int {
    set_errno(ENOSYS);
    -1
}

#[no_mangle]
pub unsafe extern "C" fn getrusage(_who: c_int, _ru: *mut c_void) -> c_int {
    set_errno(ENOSYS);
    -1
}

#[repr(C)]
pub struct Itimerval {
    pub it_interval: Timeval,
    pub it_value: Timeval,
}

#[no_mangle]
pub unsafe extern "C" fn getitimer(_w: c_int, _v: *mut Itimerval) -> c_int {
    set_errno(ENOSYS);
    -1
}

#[no_mangle]
pub unsafe extern "C" fn setitimer(_w: c_int, _v: *const Itimerval, _o: *mut Itimerval) -> c_int {
    set_errno(ENOSYS);
    -1
}

#[no_mangle]
pub unsafe extern "C" fn getloadavg(_a: *mut f64, _n: c_int) -> c_int {
    set_errno(ENOSYS);
    -1
}

#[no_mangle]
pub unsafe extern "C" fn alarm(_seconds: u32) -> u32 {
    0
}

#[no_mangle]
pub unsafe extern "C" fn umask(_mode: u32) -> u32 {
    0
}

#[no_mangle]
pub unsafe extern "C" fn chmod(_path: *const c_char, _mode: u32) -> c_int {
    set_errno(ENOENT);
    -1
}

#[no_mangle]
pub unsafe extern "C" fn fchmod(_fd: c_int, _mode: u32) -> c_int {
    set_errno(EINVAL);
    -1
}

#[no_mangle]
pub unsafe extern "C" fn fchmodat(_d: c_int, _p: *const c_char, _m: u32, _f: c_int) -> c_int {
    set_errno(ENOENT);
    -1
}

#[no_mangle]
pub unsafe extern "C" fn chdir(_path: *const c_char) -> c_int {
    set_errno(ENOENT);
    -1
}

#[no_mangle]
pub unsafe extern "C" fn getcwd(buf: *mut c_char, _size: usize) -> *mut c_char {
    if buf.is_null() {
        return ptr::null_mut();
    }
    unsafe { *buf = b'/' as c_char };
    unsafe { *buf.add(1) = 0 };
    buf
}

#[no_mangle]
pub unsafe extern "C" fn rename(a: *const c_char, b: *const c_char) -> c_int {
    crate::vfs::kern_rename(a, b)
}

#[no_mangle]
pub unsafe extern "C" fn rmdir(p: *const c_char) -> c_int {
    crate::vfs::kern_rmdir(p)
}

#[no_mangle]
pub unsafe extern "C" fn unlink(p: *const c_char) -> c_int {
    crate::vfs::kern_unlink(p)
}

#[no_mangle]
pub unsafe extern "C" fn faccessat(_d: c_int, p: *const c_char, m: c_int, _f: c_int) -> c_int {
    crate::vfs::access(p, m)
}

#[no_mangle]
pub unsafe extern "C" fn system(_cmd: *const c_char) -> c_int {
    0
}

#[no_mangle]
pub unsafe extern "C" fn abort() -> ! {
    crate::serial::write_str("abort() called\n");
    loop {
        core::hint::spin_loop();
    }
}

#[no_mangle]
pub unsafe extern "C" fn exit(_code: c_int) -> ! {
    loop {
        core::hint::spin_loop();
    }
}

#[no_mangle]
pub unsafe extern "C" fn _exit(_code: c_int) -> ! {
    loop {
        core::hint::spin_loop();
    }
}

#[no_mangle]
pub unsafe extern "C" fn ttyname_r(_fd: c_int, _buf: *mut c_char, _n: usize) -> c_int {
    set_errno(ENOTTY);
    -1
}

#[no_mangle]
pub unsafe extern "C" fn fchdir(_fd: c_int) -> c_int {
    set_errno(EINVAL);
    -1
}

#[no_mangle]
pub unsafe extern "C" fn chroot(_p: *const c_char) -> c_int {
    set_errno(EPERM);
    -1
}

#[no_mangle]
pub unsafe extern "C" fn ctermid(_s: *mut c_char) -> *mut c_char {
    ptr::null_mut()
}

#[no_mangle]
pub unsafe extern "C" fn copy_file_range(
    _i: c_int,
    _io: *mut i64,
    _o: c_int,
    _oo: *mut i64,
    _n: usize,
    _f: u32,
) -> isize {
    set_errno(ENOSYS);
    -1
}

#[no_mangle]
pub unsafe extern "C" fn splice(
    _a: c_int,
    _ao: *mut i64,
    _b: c_int,
    _bo: *mut i64,
    _n: usize,
    _f: u32,
) -> isize {
    set_errno(ENOSYS);
    -1
}

#[no_mangle]
pub unsafe extern "C" fn setuid(_u: u32) -> c_int {
    0
}

#[no_mangle]
pub unsafe extern "C" fn seteuid(_u: u32) -> c_int {
    0
}

#[no_mangle]
pub unsafe extern "C" fn setgid(_g: u32) -> c_int {
    0
}

#[no_mangle]
pub unsafe extern "C" fn setegid(_g: u32) -> c_int {
    0
}

#[no_mangle]
pub unsafe extern "C" fn setreuid(_r: u32, _e: u32) -> c_int {
    0
}

#[no_mangle]
pub unsafe extern "C" fn setregid(_r: u32, _e: u32) -> c_int {
    0
}

#[no_mangle]
pub unsafe extern "C" fn setresuid(_r: u32, _e: u32, _s: u32) -> c_int {
    0
}

#[no_mangle]
pub unsafe extern "C" fn setresgid(_r: u32, _e: u32, _s: u32) -> c_int {
    0
}

#[no_mangle]
pub unsafe extern "C" fn getresuid(_r: *mut u32, _e: *mut u32, _s: *mut u32) -> c_int {
    unsafe {
        *_r = 0;
        *_e = 0;
        *_s = 0;
    }
    0
}

#[no_mangle]
pub unsafe extern "C" fn getresgid(_r: *mut u32, _e: *mut u32, _s: *mut u32) -> c_int {
    unsafe {
        *_r = 0;
        *_e = 0;
        *_s = 0;
    }
    0
}

#[no_mangle]
pub unsafe extern "C" fn tzset() {}

#[no_mangle]
pub unsafe extern "C" fn nl_langinfo(item: c_int) -> *mut c_char {
    static mut CODESET: [c_char; 6] = [b'U' as c_char, b'T' as c_char, b'F' as c_char, b'-' as c_char, b'8' as c_char, 0];
    static mut EMPTYSTR: [c_char; 1] = [0];
    if item == 0 {
        // CODESET
        CODESET.as_mut_ptr()
    } else {
        EMPTYSTR.as_mut_ptr()
    }
}

#[no_mangle]
pub unsafe extern "C" fn grantpt(_fd: c_int) -> c_int {
    set_errno(ENOTTY);
    -1
}

#[no_mangle]
pub unsafe extern "C" fn unlockpt(_fd: c_int) -> c_int {
    set_errno(ENOTTY);
    -1
}

#[no_mangle]
pub unsafe extern "C" fn ptsname(_fd: c_int) -> *mut c_char {
    ptr::null_mut()
}

#[no_mangle]
pub unsafe extern "C" fn openpty(
    _am: *mut c_int,
    _sm: *mut c_int,
    _name: *mut c_char,
    _t: *const c_void,
    _w: *const c_void,
) -> c_int {
    set_errno(ENOSYS);
    -1
}

#[no_mangle]
pub unsafe extern "C" fn forkpty(
    _am: *mut c_int,
    _sm: *mut c_int,
    _name: *mut c_char,
    _t: *const c_void,
    _w: *const c_void,
) -> c_int {
    set_errno(ENOSYS);
    -1
}

#[no_mangle]
pub unsafe extern "C" fn login_tty(_fd: c_int) -> c_int {
    set_errno(ENOSYS);
    -1
}

#[repr(C)]
pub struct Utsname {
    pub sysname: [c_char; 65],
    pub nodename: [c_char; 65],
    pub release: [c_char; 65],
    pub version: [c_char; 65],
    pub machine: [c_char; 65],
    pub domainname: [c_char; 65],
}

fn copy_cstr(dst: *mut c_char, s: &[u8], cap: usize) {
    let n = s.len().min(cap - 1);
    for i in 0..n {
        unsafe { *dst.add(i) = s[i] as c_char };
    }
    unsafe { *dst.add(n) = 0 };
}

#[no_mangle]
pub unsafe extern "C" fn uname(u: *mut Utsname) -> c_int {
    if u.is_null() {
        set_errno(EINVAL);
        return -1;
    }
    copy_cstr((*u).sysname.as_mut_ptr(), b"InterpretiveOS", 65);
    copy_cstr((*u).nodename.as_mut_ptr(), b"kernel", 65);
    copy_cstr((*u).release.as_mut_ptr(), b"0.1.0", 65);
    copy_cstr((*u).version.as_mut_ptr(), b"kernel", 65);
    copy_cstr((*u).machine.as_mut_ptr(), b"x86_64", 65);
    copy_cstr((*u).domainname.as_mut_ptr(), b"", 65);
    0
}

#[repr(C)]
pub struct Tms {
    pub tms_utime: i64,
    pub tms_stime: i64,
    pub tms_cutime: i64,
    pub tms_cstime: i64,
}

#[no_mangle]
pub unsafe extern "C" fn times(_buf: *mut Tms) -> i64 {
    if !_buf.is_null() {
        unsafe {
            (*_buf).tms_utime = 0;
            (*_buf).tms_stime = 0;
            (*_buf).tms_cutime = 0;
            (*_buf).tms_cstime = 0;
        }
    }
    (crate::time::monotonic_ns() / 1_000_000) as i64
}

// ---------------------------------------------------------------------------
// signals (no real signals in the kernel; the signal module just needs the
// calls to succeed)
// ---------------------------------------------------------------------------

pub type Sighandler = extern "C" fn(c_int);

#[no_mangle]
pub unsafe extern "C" fn signal(_sig: c_int, _handler: Sighandler) -> Sighandler {
    unsafe { core::mem::transmute(0usize) }
}

#[no_mangle]
pub unsafe extern "C" fn raise(_sig: c_int) -> c_int {
    0
}

#[no_mangle]
pub unsafe extern "C" fn kill(_pid: c_int, _sig: c_int) -> c_int {
    0
}

#[no_mangle]
pub unsafe extern "C" fn killpg(_pgrp: c_int, _sig: c_int) -> c_int {
    0
}

#[no_mangle]
pub unsafe extern "C" fn pause() -> c_int {
    set_errno(EINTR);
    -1
}

/// musl sigset_t is 128 bytes (16 x u64).
const SIGSET_WORDS: usize = 16;

#[no_mangle]
pub unsafe extern "C" fn sigemptyset(set: *mut u64) -> c_int {
    for i in 0..SIGSET_WORDS {
        unsafe { *set.add(i) = 0 };
    }
    0
}

#[no_mangle]
pub unsafe extern "C" fn sigfillset(set: *mut u64) -> c_int {
    for i in 0..SIGSET_WORDS {
        unsafe { *set.add(i) = u64::MAX };
    }
    0
}

#[no_mangle]
pub unsafe extern "C" fn sigaddset(set: *mut u64, sig: c_int) -> c_int {
    let s = sig - 1;
    if s < 0 || s >= (SIGSET_WORDS * 64) as i32 {
        set_errno(EINVAL);
        return -1;
    }
    unsafe { *set.add((s / 64) as usize) |= 1u64 << (s % 64) };
    0
}

#[no_mangle]
pub unsafe extern "C" fn sigdelset(set: *mut u64, sig: c_int) -> c_int {
    let s = sig - 1;
    if s < 0 || s >= (SIGSET_WORDS * 64) as i32 {
        set_errno(EINVAL);
        return -1;
    }
    unsafe { *set.add((s / 64) as usize) &= !(1u64 << (s % 64)) };
    0
}

#[no_mangle]
pub unsafe extern "C" fn sigismember(set: *const u64, sig: c_int) -> c_int {
    let s = sig - 1;
    if s < 0 || s >= (SIGSET_WORDS * 64) as i32 {
        set_errno(EINVAL);
        return -1;
    }
    ((unsafe { *set.add((s / 64) as usize) } >> (s % 64)) & 1) as c_int
}

#[repr(C)]
pub struct Sigaction {
    pub handler: usize, // sa_handler/sa_sigaction union
    pub mask: [u64; SIGSET_WORDS],
    pub flags: c_int,
    pub restorer: usize,
}

#[no_mangle]
pub unsafe extern "C" fn sigaction(
    _sig: c_int,
    _act: *const Sigaction,
    _old: *mut Sigaction,
) -> c_int {
    0
}

#[no_mangle]
pub unsafe extern "C" fn sigpending(_set: *mut u64) -> c_int {
    0
}

#[no_mangle]
pub unsafe extern "C" fn sigaltstack(_ss: *const c_void, _oss: *mut c_void) -> c_int {
    0
}

#[no_mangle]
pub unsafe extern "C" fn sigwait(_set: *const u64, _sig: *mut c_int) -> c_int {
    0
}

#[no_mangle]
pub unsafe extern "C" fn sigwaitinfo(_set: *const u64, _info: *mut c_void) -> c_int {
    set_errno(EINTR);
    -1
}

#[no_mangle]
pub unsafe extern "C" fn sigtimedwait(
    _set: *const u64,
    _info: *mut c_void,
    _t: *const c_void,
) -> c_int {
    set_errno(EINTR);
    -1
}

#[no_mangle]
pub unsafe extern "C" fn __libc_current_sigrtmin() -> c_int {
    32
}

#[no_mangle]
pub unsafe extern "C" fn __libc_current_sigrtmax() -> c_int {
    64
}

// ---------------------------------------------------------------------------
// file descriptors (read/write/close/lseek/stat live in vfs.rs)
// ---------------------------------------------------------------------------

pub const FD_URANDOM: c_int = 100;
pub const FD_STDIN: c_int = 0;
pub const FD_STDOUT: c_int = 1;
pub const FD_STDERR: c_int = 2;

#[no_mangle]
pub unsafe extern "C" fn dup(fd: c_int) -> c_int {
    if fd == FD_STDOUT || fd == FD_STDERR {
        fd
    } else {
        set_errno(EBADF);
        -1
    }
}

#[no_mangle]
pub unsafe extern "C" fn dup2(oldfd: c_int, newfd: c_int) -> c_int {
    if oldfd == FD_STDOUT || oldfd == FD_STDERR {
        newfd
    } else {
        set_errno(EBADF);
        -1
    }
}

#[no_mangle]
pub unsafe extern "C" fn dup3(oldfd: c_int, newfd: c_int, _flags: c_int) -> c_int {
    dup2(oldfd, newfd)
}

#[no_mangle]
pub unsafe extern "C" fn tcgetpgrp(_fd: c_int) -> c_int {
    set_errno(ENOTTY);
    -1
}

#[no_mangle]
pub unsafe extern "C" fn tcsetpgrp(_fd: c_int, _pgrp: c_int) -> c_int {
    set_errno(ENOTTY);
    -1
}

#[no_mangle]
pub unsafe extern "C" fn lockf(_fd: c_int, _cmd: c_int, _len: i64) -> c_int {
    set_errno(ENOSYS);
    -1
}

#[no_mangle]
pub unsafe extern "C" fn preadv2(
    _fd: c_int,
    _iov: *const c_void,
    _n: c_int,
    _off: i64,
    _flags: c_int,
) -> isize {
    set_errno(ENOSYS);
    -1
}

#[no_mangle]
pub unsafe extern "C" fn pwritev2(
    _fd: c_int,
    _iov: *const c_void,
    _n: c_int,
    _off: i64,
    _flags: c_int,
) -> isize {
    set_errno(ENOSYS);
    -1
}

#[no_mangle]
pub unsafe extern "C" fn posix_fallocate(_fd: c_int, _off: i64, _len: i64) -> c_int {
    ENOSYS
}

#[no_mangle]
pub unsafe extern "C" fn posix_fadvise(_fd: c_int, _off: i64, _len: i64, _adv: c_int) -> c_int {
    ENOSYS
}

#[no_mangle]
pub unsafe extern "C" fn confstr(_name: c_int, _buf: *mut c_char, _n: usize) -> usize {
    set_errno(EINVAL);
    0
}

#[no_mangle]
pub unsafe extern "C" fn setns(_fd: c_int, _nstype: c_int) -> c_int {
    set_errno(EPERM);
    -1
}

#[no_mangle]
pub unsafe extern "C" fn unshare(_flags: c_int) -> c_int {
    set_errno(EPERM);
    -1
}

#[no_mangle]
pub unsafe extern "C" fn atoi(s: *const c_char) -> c_int {
    let mut i = 0usize;
    let mut neg = false;
    let mut c = unsafe { *s.add(i) } as u8;
    while c == b' ' || c == b'\t' {
        i += 1;
        c = unsafe { *s.add(i) } as u8;
    }
    if c == b'+' || c == b'-' {
        neg = c == b'-';
        i += 1;
    }
    let mut v: i32 = 0;
    loop {
        let c = unsafe { *s.add(i) } as u8;
        if !(b'0'..=b'9').contains(&c) {
            break;
        }
        v = v.wrapping_mul(10).wrapping_add((c - b'0') as i32);
        i += 1;
    }
    if neg {
        -v
    } else {
        v
    }
}

#[no_mangle]
pub unsafe extern "C" fn abs(v: c_int) -> c_int {
    v.abs()
}

#[no_mangle]
pub unsafe extern "C" fn getpwuid_r(
    _uid: u32,
    _pwd: *mut c_void,
    _buf: *mut c_char,
    _n: usize,
    _out: *mut *mut c_void,
) -> c_int {
    set_errno(ENOENT);
    unsafe { *_out = ptr::null_mut() };
    ENOENT
}

#[no_mangle]
pub unsafe extern "C" fn getpwnam_r(
    _name: *const c_char,
    _pwd: *mut c_void,
    _buf: *mut c_char,
    _n: usize,
    _out: *mut *mut c_void,
) -> c_int {
    set_errno(ENOENT);
    unsafe { *_out = ptr::null_mut() };
    ENOENT
}

#[no_mangle]
pub unsafe extern "C" fn setpwent() {}

#[no_mangle]
pub unsafe extern "C" fn getpwent() -> *mut c_void {
    ptr::null_mut()
}

#[no_mangle]
pub unsafe extern "C" fn endpwent() {}

#[no_mangle]
pub unsafe extern "C" fn isatty(fd: c_int) -> c_int {
    // The serial console is a terminal: line-buffered stdout.
    if fd == FD_STDIN || fd == FD_STDOUT || fd == FD_STDERR {
        1
    } else {
        0
    }
}

#[no_mangle]
pub unsafe extern "C" fn pread(_fd: c_int, _buf: *mut c_void, _n: usize, _off: i64) -> isize {
    set_errno(ENOSYS);
    -1
}

#[no_mangle]
pub unsafe extern "C" fn pwrite(_fd: c_int, _buf: *const c_void, _n: usize, _off: i64) -> isize {
    set_errno(ENOSYS);
    -1
}

#[no_mangle]
pub unsafe extern "C" fn readv(_fd: c_int, _iov: *const c_void, _n: c_int) -> isize {
    set_errno(ENOSYS);
    -1
}

#[no_mangle]
pub unsafe extern "C" fn writev(_fd: c_int, _iov: *const c_void, _n: c_int) -> isize {
    set_errno(ENOSYS);
    -1
}

#[no_mangle]
pub unsafe extern "C" fn pipe(_fds: *mut c_int) -> c_int {
    set_errno(ENOSYS);
    -1
}

#[no_mangle]
pub unsafe extern "C" fn pipe2(_fds: *mut c_int, _flags: c_int) -> c_int {
    set_errno(ENOSYS);
    -1
}

#[no_mangle]
pub unsafe extern "C" fn fsync(fd: c_int) -> c_int {
    if fd == FD_STDOUT || fd == FD_STDERR {
        0
    } else {
        set_errno(EINVAL);
        -1
    }
}

#[no_mangle]
pub unsafe extern "C" fn fdatasync(fd: c_int) -> c_int {
    fsync(fd)
}

#[no_mangle]
pub unsafe extern "C" fn truncate(p: *const c_char, len: i64) -> c_int {
    crate::vfs::kern_truncate(p, len)
}

#[no_mangle]
pub unsafe extern "C" fn ftruncate(fd: c_int, len: i64) -> c_int {
    crate::vfs::kern_ftruncate(fd, len)
}

/// mmap for obmalloc arenas: allocate `size` (+ guard for alignment) from the
/// C heap and return a 4096-aligned pointer; munmap recovers the original
/// allocation.
#[no_mangle]
pub unsafe extern "C" fn mmap(
    _addr: *mut c_void,
    size: usize,
    _prot: c_int,
    _flags: c_int,
    _fd: c_int,
    _off: i64,
) -> *mut c_void {
    const PAGE: usize = 4096;
    if size == 0 {
        set_errno(EINVAL);
        return usize::MAX as *mut c_void;
    }
    let orig = unsafe { malloc_impl(size + PAGE) };
    if orig.is_null() {
        set_errno(ENOMEM);
        return usize::MAX as *mut c_void;
    }
    let aligned = ((orig as usize) + PAGE - 1) & !(PAGE - 1);
    unsafe {
        *((aligned - 8) as *mut usize) = orig as usize;
    }
    aligned as *mut c_void
}

#[no_mangle]
pub unsafe extern "C" fn munmap(addr: *mut c_void, _size: usize) -> c_int {
    if addr.is_null() {
        return 0;
    }
    let orig = unsafe { *((addr as usize - 8) as *const usize) };
    unsafe { free_impl(orig as *mut c_void) };
    0
}

/// getrandom: fill from the TSC-based PRNG (same generator as /dev/urandom).
#[no_mangle]
pub unsafe extern "C" fn getrandom(buf: *mut c_void, len: usize, _flags: u32) -> isize {
    let mut state = crate::time::rdtsc();
    let out = buf as *mut u8;
    let n = len.min(1024);
    for i in 0..n {
        state ^= state >> 12;
        state ^= state << 25;
        state ^= state >> 27;
        let v = state.wrapping_mul(0x2545F4914F6CDD1D);
        unsafe { *out.add(i) = (v >> 56) as u8 };
    }
    n as isize
}

#[no_mangle]
pub unsafe extern "C" fn getauxval(_type: u64) -> u64 {
    0
}

/// musl _SC_* constants (x86-64).
const _SC_ARG_MAX: c_int = 0;
const _SC_CHILD_MAX: c_int = 1;
const _SC_CLK_TCK: c_int = 2;
const _SC_NGROUPS_MAX: c_int = 3;
const _SC_OPEN_MAX: c_int = 4;
const _SC_STREAM_MAX: c_int = 5;
const _SC_TZNAME_MAX: c_int = 6;
const _SC_PAGESIZE: c_int = 30;
const _SC_NPROCESSORS_CONF: c_int = 83;
const _SC_NPROCESSORS_ONLN: c_int = 84;
const _SC_PHYS_PAGES: c_int = 85;

#[no_mangle]
pub unsafe extern "C" fn sysconf(name: c_int) -> i64 {
    match name {
        _SC_ARG_MAX => 131072,
        _SC_CHILD_MAX => 64,
        _SC_CLK_TCK => 100,
        _SC_NGROUPS_MAX => 0,
        _SC_OPEN_MAX => 1024,
        _SC_STREAM_MAX => 16,
        _SC_TZNAME_MAX => 6,
        _SC_PAGESIZE => 4096,
        _SC_NPROCESSORS_CONF => 1,
        _SC_NPROCESSORS_ONLN => 1,
        _SC_PHYS_PAGES => 64 * 1024,
        _ => {
            set_errno(EINVAL);
            -1
        }
    }
}

#[no_mangle]
pub unsafe extern "C" fn pathconf(_path: *const c_char, _name: c_int) -> i64 {
    set_errno(EINVAL);
    -1
}

#[no_mangle]
pub unsafe extern "C" fn fpathconf(_fd: c_int, _name: c_int) -> i64 {
    set_errno(EINVAL);
    -1
}

#[no_mangle]
pub unsafe extern "C" fn __sched_cpucount(setsize: usize, set: *const u8) -> c_int {
    let mut count = 0u32;
    for i in 0..setsize {
        count += unsafe { *set.add(i) }.count_ones();
    }
    count as c_int
}

// ---------------------------------------------------------------------------
// stat (musl x86-64 layout, 144 bytes)
// ---------------------------------------------------------------------------

#[repr(C)]
#[derive(Clone, Copy)]
pub struct Timespec {
    pub tv_sec: i64,
    pub tv_nsec: i64,
}

#[repr(C)]
pub struct Stat {
    pub st_dev: u64,
    pub st_ino: u64,
    pub st_nlink: u64,
    pub st_mode: u32,
    pub st_uid: u32,
    pub st_gid: u32,
    pub __pad0: u32,
    pub st_rdev: u64,
    pub st_size: i64,
    pub st_blksize: i64,
    pub st_blocks: i64,
    pub st_atim: Timespec,
    pub st_mtim: Timespec,
    pub st_ctim: Timespec,
    pub __unused: [i64; 3],
}

// stat()/fstat()/lstat()/access() live in vfs.rs (initramfs-backed).

const AT_FDCWD: c_int = -100;

#[no_mangle]
pub unsafe extern "C" fn fstatat(
    dirfd: c_int,
    path: *const c_char,
    st: *mut Stat,
    _flags: c_int,
) -> c_int {
    if dirfd == AT_FDCWD {
        return crate::vfs::stat(path, st);
    }
    set_errno(ENOENT);
    -1
}

#[no_mangle]
pub unsafe extern "C" fn statvfs(_p: *const c_char, _buf: *mut c_void) -> c_int {
    set_errno(ENOSYS);
    -1
}

#[no_mangle]
pub unsafe extern "C" fn fstatvfs(_fd: c_int, _buf: *mut c_void) -> c_int {
    set_errno(ENOSYS);
    -1
}

// ---------------------------------------------------------------------------
// filesystem operations (all stubs: no filesystem yet)
// ---------------------------------------------------------------------------

#[no_mangle]
pub unsafe extern "C" fn mkdir(p: *const c_char, mode: u32) -> c_int {
    crate::vfs::kern_mkdir(p, mode)
}

#[no_mangle]
pub unsafe extern "C" fn mkdirat(_d: c_int, p: *const c_char, mode: u32) -> c_int {
    crate::vfs::kern_mkdir(p, mode)
}

#[no_mangle]
pub unsafe extern "C" fn mknod(_p: *const c_char, _m: u32, _d: u64) -> c_int {
    set_errno(ENOENT);
    -1
}

#[no_mangle]
pub unsafe extern "C" fn mknodat(_d: c_int, _p: *const c_char, _m: u32, _r: u64) -> c_int {
    set_errno(ENOENT);
    -1
}

#[no_mangle]
pub unsafe extern "C" fn mkfifo(_p: *const c_char, _mode: u32) -> c_int {
    set_errno(ENOENT);
    -1
}

#[no_mangle]
pub unsafe extern "C" fn mkfifoat(_d: c_int, _p: *const c_char, _mode: u32) -> c_int {
    set_errno(ENOENT);
    -1
}

#[no_mangle]
pub unsafe extern "C" fn unlinkat(_d: c_int, p: *const c_char, _f: c_int) -> c_int {
    crate::vfs::kern_unlink(p)
}

#[no_mangle]
pub unsafe extern "C" fn renameat(_a: c_int, p: *const c_char, _b: c_int, q: *const c_char) -> c_int {
    crate::vfs::kern_rename(p, q)
}

#[no_mangle]
pub unsafe extern "C" fn link(_a: *const c_char, _b: *const c_char) -> c_int {
    set_errno(ENOENT);
    -1
}

#[no_mangle]
pub unsafe extern "C" fn linkat(_d: c_int, _a: *const c_char, _e: c_int, _b: *const c_char, _f: c_int) -> c_int {
    set_errno(ENOENT);
    -1
}

#[no_mangle]
pub unsafe extern "C" fn symlink(_a: *const c_char, _b: *const c_char) -> c_int {
    set_errno(ENOENT);
    -1
}

#[no_mangle]
pub unsafe extern "C" fn symlinkat(_a: *const c_char, _d: c_int, _b: *const c_char) -> c_int {
    set_errno(ENOENT);
    -1
}

#[no_mangle]
pub unsafe extern "C" fn readlink(_p: *const c_char, _buf: *mut c_char, _n: usize) -> isize {
    set_errno(ENOENT);
    -1
}

#[no_mangle]
pub unsafe extern "C" fn readlinkat(_d: c_int, _p: *const c_char, _buf: *mut c_char, _n: usize) -> isize {
    set_errno(ENOENT);
    -1
}

#[no_mangle]
pub unsafe extern "C" fn realpath(_p: *const c_char, _buf: *mut c_char) -> *mut c_char {
    set_errno(ENOENT);
    ptr::null_mut()
}

#[no_mangle]
pub unsafe extern "C" fn chown(_p: *const c_char, _u: u32, _g: u32) -> c_int {
    set_errno(ENOENT);
    -1
}

#[no_mangle]
pub unsafe extern "C" fn fchown(_fd: c_int, _u: u32, _g: u32) -> c_int {
    set_errno(EINVAL);
    -1
}

#[no_mangle]
pub unsafe extern "C" fn lchown(_p: *const c_char, _u: u32, _g: u32) -> c_int {
    set_errno(ENOENT);
    -1
}

#[no_mangle]
pub unsafe extern "C" fn fchownat(_d: c_int, _p: *const c_char, _u: u32, _g: u32, _f: c_int) -> c_int {
    set_errno(ENOENT);
    -1
}

// opendir/readdir/closedir live in vfs.rs.

#[no_mangle]
pub unsafe extern "C" fn fdopendir(_fd: c_int) -> *mut c_void {
    set_errno(ENOENT);
    ptr::null_mut()
}

#[no_mangle]
pub unsafe extern "C" fn rewinddir(_dir: *mut c_void) {}

#[no_mangle]
pub unsafe extern "C" fn utimensat(
    _d: c_int,
    _p: *const c_char,
    _t: *const c_void,
    _f: c_int,
) -> c_int {
    set_errno(ENOENT);
    -1
}

#[no_mangle]
pub unsafe extern "C" fn futimens(_fd: c_int, _t: *const c_void) -> c_int {
    set_errno(EINVAL);
    -1
}

#[repr(C)]
pub struct Utimbuf {
    pub actime: i64,
    pub modtime: i64,
}

#[no_mangle]
pub unsafe extern "C" fn utime(_p: *const c_char, _t: *mut Utimbuf) -> c_int {
    set_errno(ENOENT);
    -1
}

#[no_mangle]
pub unsafe extern "C" fn sendfile(_o: c_int, _i: c_int, _o2: *mut i64, _n: usize) -> isize {
    set_errno(ENOSYS);
    -1
}

#[no_mangle]
pub unsafe extern "C" fn sync() {}

#[no_mangle]
pub unsafe extern "C" fn getxattr(_p: *const c_char, _n: *const c_char, _v: *mut c_void, _s: usize) -> isize {
    set_errno(ENOSYS);
    -1
}

#[no_mangle]
pub unsafe extern "C" fn setxattr(_p: *const c_char, _n: *const c_char, _v: *const c_void, _s: usize, _f: c_int) -> c_int {
    set_errno(ENOSYS);
    -1
}

#[no_mangle]
pub unsafe extern "C" fn listxattr(_p: *const c_char, _v: *mut c_char, _s: usize) -> isize {
    set_errno(ENOSYS);
    -1
}

#[no_mangle]
pub unsafe extern "C" fn removexattr(_p: *const c_char, _n: *const c_char) -> c_int {
    set_errno(ENOSYS);
    -1
}

#[no_mangle]
pub unsafe extern "C" fn lgetxattr(_p: *const c_char, _n: *const c_char, _v: *mut c_void, _s: usize) -> isize {
    set_errno(ENOSYS);
    -1
}

#[no_mangle]
pub unsafe extern "C" fn lsetxattr(_p: *const c_char, _n: *const c_char, _v: *const c_void, _s: usize, _f: c_int) -> c_int {
    set_errno(ENOSYS);
    -1
}

#[no_mangle]
pub unsafe extern "C" fn llistxattr(_p: *const c_char, _v: *mut c_char, _s: usize) -> isize {
    set_errno(ENOSYS);
    -1
}

#[no_mangle]
pub unsafe extern "C" fn lremovexattr(_p: *const c_char, _n: *const c_char) -> c_int {
    set_errno(ENOSYS);
    -1
}

#[no_mangle]
pub unsafe extern "C" fn fgetxattr(_fd: c_int, _n: *const c_char, _v: *mut c_void, _s: usize) -> isize {
    set_errno(ENOSYS);
    -1
}

#[no_mangle]
pub unsafe extern "C" fn fsetxattr(_fd: c_int, _n: *const c_char, _v: *const c_void, _s: usize, _f: c_int) -> c_int {
    set_errno(ENOSYS);
    -1
}

#[no_mangle]
pub unsafe extern "C" fn flistxattr(_fd: c_int, _v: *mut c_char, _s: usize) -> isize {
    set_errno(ENOSYS);
    -1
}

#[no_mangle]
pub unsafe extern "C" fn fremovexattr(_fd: c_int, _n: *const c_char) -> c_int {
    set_errno(ENOSYS);
    -1
}

#[no_mangle]
pub unsafe extern "C" fn eventfd(_init: u32, _flags: c_int) -> c_int {
    set_errno(ENOSYS);
    -1
}

#[no_mangle]
pub unsafe extern "C" fn eventfd_read(_fd: c_int, _v: *mut u64) -> c_int {
    set_errno(ENOSYS);
    -1
}

#[no_mangle]
pub unsafe extern "C" fn eventfd_write(_fd: c_int, _v: u64) -> c_int {
    set_errno(ENOSYS);
    -1
}

#[no_mangle]
pub unsafe extern "C" fn memfd_create(_n: *const c_char, _f: u32) -> c_int {
    set_errno(ENOSYS);
    -1
}

// ---------------------------------------------------------------------------
// process spawning stubs
// ---------------------------------------------------------------------------

#[no_mangle]
pub unsafe extern "C" fn fork() -> c_int {
    set_errno(ENOSYS);
    -1
}

#[no_mangle]
pub unsafe extern "C" fn execv(_p: *const c_char, _argv: *const *mut c_char) -> c_int {
    set_errno(ENOENT);
    -1
}

#[no_mangle]
pub unsafe extern "C" fn execve(
    _p: *const c_char,
    _argv: *const *mut c_char,
    _envp: *const *mut c_char,
) -> c_int {
    set_errno(ENOENT);
    -1
}

#[no_mangle]
pub unsafe extern "C" fn fexecve(_fd: c_int, _argv: *const *mut c_char, _envp: *const *mut c_char) -> c_int {
    set_errno(ENOSYS);
    -1
}

#[no_mangle]
pub unsafe extern "C" fn wait(_s: *mut c_int) -> c_int {
    set_errno(ECHILD);
    -1
}

#[no_mangle]
pub unsafe extern "C" fn waitpid(_p: c_int, _s: *mut c_int, _o: c_int) -> c_int {
    set_errno(ECHILD);
    -1
}

#[no_mangle]
pub unsafe extern "C" fn wait3(_s: *mut c_int, _o: c_int, _r: *mut c_void) -> c_int {
    set_errno(ECHILD);
    -1
}

#[no_mangle]
pub unsafe extern "C" fn wait4(_p: c_int, _s: *mut c_int, _o: c_int, _r: *mut c_void) -> c_int {
    set_errno(ECHILD);
    -1
}

#[no_mangle]
pub unsafe extern "C" fn waitid(_t: c_int, _p: u32, _i: *mut c_void, _o: c_int) -> c_int {
    set_errno(ECHILD);
    -1
}

#[no_mangle]
pub unsafe extern "C" fn posix_spawn(_a: *mut c_void, _p: *const c_char, _f: *const c_void, _s: *const c_void, _v: *const *mut c_char, _e: *const *mut c_char) -> c_int {
    ENOSYS
}

#[no_mangle]
pub unsafe extern "C" fn posix_spawnp(_a: *mut c_void, _p: *const c_char, _f: *const c_void, _s: *const c_void, _v: *const *mut c_char, _e: *const *mut c_char) -> c_int {
    ENOSYS
}

#[no_mangle]
pub unsafe extern "C" fn posix_spawn_file_actions_init(_a: *mut c_void) -> c_int {
    0
}

#[no_mangle]
pub unsafe extern "C" fn posix_spawn_file_actions_destroy(_a: *mut c_void) -> c_int {
    0
}

#[no_mangle]
pub unsafe extern "C" fn posix_spawn_file_actions_addclose(_a: *mut c_void, _f: c_int) -> c_int {
    0
}

#[no_mangle]
pub unsafe extern "C" fn posix_spawn_file_actions_adddup2(_a: *mut c_void, _f: c_int, _g: c_int) -> c_int {
    0
}

#[no_mangle]
pub unsafe extern "C" fn posix_spawn_file_actions_addopen(_a: *mut c_void, _f: c_int, _p: *const c_char, _o: c_int, _m: u32) -> c_int {
    0
}

#[no_mangle]
pub unsafe extern "C" fn posix_spawnattr_init(_a: *mut c_void) -> c_int {
    0
}

#[no_mangle]
pub unsafe extern "C" fn posix_spawnattr_destroy(_a: *mut c_void) -> c_int {
    0
}

#[no_mangle]
pub unsafe extern "C" fn posix_spawnattr_setflags(_a: *mut c_void, _f: i16) -> c_int {
    0
}

#[no_mangle]
pub unsafe extern "C" fn posix_spawnattr_setpgroup(_a: *mut c_void, _p: c_int) -> c_int {
    0
}

#[no_mangle]
pub unsafe extern "C" fn posix_spawnattr_setschedparam(_a: *mut c_void, _p: *const c_void) -> c_int {
    0
}

#[no_mangle]
pub unsafe extern "C" fn posix_spawnattr_setschedpolicy(_a: *mut c_void, _p: c_int) -> c_int {
    0
}

#[no_mangle]
pub unsafe extern "C" fn posix_spawnattr_setsigdefault(_a: *mut c_void, _s: *const c_void) -> c_int {
    0
}

#[no_mangle]
pub unsafe extern "C" fn posix_spawnattr_setsigmask(_a: *mut c_void, _s: *const c_void) -> c_int {
    0
}

// ---------------------------------------------------------------------------
// time
// ---------------------------------------------------------------------------

const CLOCK_REALTIME: i32 = 0;
const CLOCK_MONOTONIC: i32 = 1;

#[no_mangle]
pub unsafe extern "C" fn clock_gettime(clockid: c_int, ts: *mut Timespec) -> c_int {
    let mono_ns = crate::time::monotonic_ns();
    let sec = match clockid {
        CLOCK_MONOTONIC => mono_ns / 1_000_000_000,
        _ => 1_700_000_000 + (mono_ns / 1_000_000_000),
    };
    unsafe {
        (*ts).tv_sec = sec as i64;
        (*ts).tv_nsec = (mono_ns % 1_000_000_000) as i64;
    }
    0
}

#[no_mangle]
pub unsafe extern "C" fn clock_getres(_clockid: c_int, ts: *mut Timespec) -> c_int {
    unsafe {
        (*ts).tv_sec = 0;
        (*ts).tv_nsec = 1;
    }
    0
}

#[no_mangle]
pub unsafe extern "C" fn clock_settime(_clockid: c_int, _ts: *const Timespec) -> c_int {
    set_errno(EINVAL);
    -1
}

#[no_mangle]
pub unsafe extern "C" fn clock_nanosleep(
    _clockid: c_int,
    _flags: c_int,
    req: *const Timespec,
    _rem: *mut Timespec,
) -> c_int {
    let start = crate::time::monotonic_ns();
    let target = start + unsafe { (*req).tv_sec as u64 } * 1_000_000_000
        + unsafe { (*req).tv_nsec as u64 };
    while crate::time::monotonic_ns() < target {
        crate::sched::sched_yield();
    }
    0
}

#[repr(C)]
pub struct Timeval {
    pub tv_sec: i64,
    pub tv_usec: i64,
}

#[no_mangle]
pub unsafe extern "C" fn gettimeofday(tv: *mut Timeval, _tz: *mut c_void) -> c_int {
    let mono_ns = crate::time::monotonic_ns();
    unsafe {
        (*tv).tv_sec = 1_700_000_000 + (mono_ns / 1_000_000_000) as i64;
        (*tv).tv_usec = ((mono_ns / 1_000) % 1_000_000) as i64;
    }
    0
}

#[no_mangle]
pub unsafe extern "C" fn time(t: *mut i64) -> i64 {
    let v = 1_700_000_000 + (crate::time::monotonic_ns() / 1_000_000_000) as i64;
    if !t.is_null() {
        unsafe { *t = v };
    }
    v
}

#[no_mangle]
pub unsafe extern "C" fn clock() -> i64 {
    (crate::time::monotonic_ns() / 1_000_000) as i64
}

/// musl struct tm (56 bytes): 9 ints, pad, long tm_gmtoff, const char *tm_zone.
#[repr(C)]
#[derive(Clone, Copy)]
pub struct Tm {
    pub tm_sec: c_int,
    pub tm_min: c_int,
    pub tm_hour: c_int,
    pub tm_mday: c_int,
    pub tm_mon: c_int,
    pub tm_year: c_int,
    pub tm_wday: c_int,
    pub tm_yday: c_int,
    pub tm_isdst: c_int,
    pub tm_gmtoff: i64,
    pub tm_zone: *mut c_char,
}

/// Days from civil date (Howard Hinnant).
fn days_from_civil(y: i64, m: i64, d: i64) -> i64 {
    let y = if m <= 2 { y - 1 } else { y };
    let era = if y >= 0 { y } else { y - 399 } / 400;
    let yoe = y - era * 400;
    let mp = if m > 2 { m - 3 } else { m + 9 };
    let doy = (153 * mp + 2) / 5 + d - 1;
    let doe = yoe * 365 + yoe / 4 - yoe / 100 + doy;
    era * 146097 + doe - 719468
}

fn civil_from_days(z: i64) -> (i64, i64, i64) {
    let z = z + 719468;
    let era = if z >= 0 { z } else { z - 146096 } / 146097;
    let doe = z - era * 146097;
    let yoe = (doe - doe / 1460 + doe / 36524 - doe / 146096) / 365;
    let y = yoe + era * 400;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let d = doy - (153 * mp + 2) / 5 + 1;
    let m = if mp < 10 { mp + 3 } else { mp - 9 };
    (if m <= 2 { y + 1 } else { y }, m, d)
}

static mut UTC_ZONE: [c_char; 4] = [b'U' as c_char, b'T' as c_char, b'C' as c_char, 0];

fn fill_tm(tm: *mut Tm, t: i64) {
    let days = t.div_euclid(86400);
    let secs = t.rem_euclid(86400);
    let (y, m, d) = civil_from_days(days);
    unsafe {
        (*tm).tm_sec = (secs % 60) as c_int;
        (*tm).tm_min = ((secs / 60) % 60) as c_int;
        (*tm).tm_hour = (secs / 3600) as c_int;
        (*tm).tm_mday = d as c_int;
        (*tm).tm_mon = (m - 1) as c_int;
        (*tm).tm_year = (y - 1900) as c_int;
        (*tm).tm_wday = ((days + 4).rem_euclid(7)) as c_int;
        (*tm).tm_yday = (days - days_from_civil(y, 1, 1)) as c_int;
        (*tm).tm_isdst = 0;
        (*tm).tm_gmtoff = 0;
        (*tm).tm_zone = UTC_ZONE.as_mut_ptr();
    }
}

#[no_mangle]
pub unsafe extern "C" fn gmtime_r(t: *const i64, tm: *mut Tm) -> *mut Tm {
    if tm.is_null() || t.is_null() {
        return ptr::null_mut();
    }
    fill_tm(tm, unsafe { *t });
    tm
}

#[no_mangle]
pub unsafe extern "C" fn localtime_r(t: *const i64, tm: *mut Tm) -> *mut Tm {
    // No timezone support: UTC.
    gmtime_r(t, tm)
}

#[no_mangle]
pub unsafe extern "C" fn mktime(tm: *mut Tm) -> i64 {
    let t = unsafe { *tm };
    let y = t.tm_year as i64 + 1900;
    let m = t.tm_mon as i64 + 1;
    let d = t.tm_mday as i64;
    let days = days_from_civil(y, m, d);
    let secs = days * 86400
        + t.tm_hour as i64 * 3600
        + t.tm_min as i64 * 60
        + t.tm_sec as i64;
    unsafe {
        (*tm).tm_wday = ((days + 4).rem_euclid(7)) as c_int;
        (*tm).tm_yday = (days - days_from_civil(y, 1, 1)) as c_int;
        (*tm).tm_isdst = 0;
    }
    secs
}

// ---------------------------------------------------------------------------
// strftime
// ---------------------------------------------------------------------------

static MONTHS_FULL: [&str; 12] = [
    "January", "February", "March", "April", "May", "June", "July", "August",
    "September", "October", "November", "December",
];
static MONTHS_SHORT: [&str; 12] = [
    "Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
];
static DAYS_FULL: [&str; 7] = [
    "Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday",
];
static DAYS_SHORT: [&str; 7] = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];

unsafe fn append_str(out: &mut [u8], pos: &mut usize, s: &str) {
    let mut rem = out.len().saturating_sub(*pos);
    for b in s.bytes() {
        if rem == 0 {
            return;
        }
        out[*pos] = b;
        *pos += 1;
        rem -= 1;
    }
}

unsafe fn append_num(out: &mut [u8], pos: &mut usize, mut v: i64, width: usize, pad: u8) {
    if v < 0 {
        append_str(out, pos, "-");
        v = -v;
    }
    let mut digits = [0u8; 20];
    let mut n = 0;
    if v == 0 {
        digits[n] = b'0';
        n = 1;
    } else {
        while v > 0 {
            digits[n] = (b'0' + (v % 10) as u8);
            n += 1;
            v /= 10;
        }
    }
    while n < width {
        append_str(out, pos, core::str::from_utf8_unchecked(&[pad; 1]));
        n += 1;
    }
    for i in (0..n).rev() {
        append_str(out, pos, core::str::from_utf8_unchecked(&[digits[i]; 1]));
    }
}

#[no_mangle]
pub unsafe extern "C" fn strftime(
    out: *mut c_char,
    max: usize,
    fmt: *const c_char,
    tm: *const Tm,
) -> usize {
    let mut buf = [0u8; 256];
    let mut pos = 0usize;
    let fmt_s = core::slice::from_raw_parts(fmt as *const u8, cstr_len(fmt as *const c_char));
    let t = unsafe { &*tm };
    let mut i = 0;
    while i < fmt_s.len() {
        let c = fmt_s[i];
        if c != b'%' || i + 1 >= fmt_s.len() {
            if pos < 255 {
                buf[pos] = c;
                pos += 1;
            }
            i += 1;
            continue;
        }
        let spec = fmt_s[i + 1];
        i += 2;
        let (_, num_width): (bool, usize) = match spec {
            b'0' => {
                let s2 = fmt_s.get(i);
                if let Some(&s2c) = s2 {
                    if let Some(w) = (s2c as char).to_digit(10) {
                        i += 1;
                        (false, w as usize)
                    } else {
                        (false, 0)
                    }
                } else {
                    (false, 0)
                }
            }
            b'-' => (false, 0),
            _ => (true, 0),
        };
        match spec {
            b'Y' => append_num(&mut buf, &mut pos, t.tm_year as i64 + 1900, num_width.max(4), b'0'),
            b'y' => append_num(&mut buf, &mut pos, (t.tm_year + 1900).rem_euclid(100) as i64, num_width.max(2), b'0'),
            b'm' => append_num(&mut buf, &mut pos, t.tm_mon as i64 + 1, num_width.max(2), b'0'),
            b'd' => append_num(&mut buf, &mut pos, t.tm_mday as i64, num_width.max(2), b'0'),
            b'H' => append_num(&mut buf, &mut pos, t.tm_hour as i64, num_width.max(2), b'0'),
            b'I' => {
                let mut h = t.tm_hour % 12;
                if h == 0 {
                    h = 12;
                }
                append_num(&mut buf, &mut pos, h as i64, num_width.max(2), b'0')
            }
            b'M' => append_num(&mut buf, &mut pos, t.tm_min as i64, num_width.max(2), b'0'),
            b'S' => append_num(&mut buf, &mut pos, t.tm_sec as i64, num_width.max(2), b'0'),
            b'j' => append_num(&mut buf, &mut pos, t.tm_yday as i64 + 1, num_width.max(3), b'0'),
            b'w' => append_num(&mut buf, &mut pos, t.tm_wday as i64, 1, b'0'),
            b'p' => append_str(&mut buf, &mut pos, if t.tm_hour < 12 { "AM" } else { "PM" }),
            b'a' => append_str(&mut buf, &mut pos, DAYS_SHORT[t.tm_wday as usize]),
            b'A' => append_str(&mut buf, &mut pos, DAYS_FULL[t.tm_wday as usize]),
            b'b' | b'h' => append_str(&mut buf, &mut pos, MONTHS_SHORT[t.tm_mon as usize]),
            b'B' => append_str(&mut buf, &mut pos, MONTHS_FULL[t.tm_mon as usize]),
            b'%' => append_str(&mut buf, &mut pos, "%"),
            b'n' => append_str(&mut buf, &mut pos, "\n"),
            b't' => append_str(&mut buf, &mut pos, "\t"),
            _ => append_str(&mut buf, &mut pos, "%?"),
        }
    }
    let len = pos.min(max.saturating_sub(1));
    ptr::copy_nonoverlapping(buf.as_ptr(), out as *mut u8, len);
    unsafe { *out.add(len) = 0 };
    len
}

#[no_mangle]
pub unsafe extern "C" fn wcsftime(
    _out: *mut u32,
    _max: usize,
    _fmt: *const u32,
    _tm: *const Tm,
) -> usize {
    0
}

// ---------------------------------------------------------------------------
// gettext stubs (plain names for musl)
// ---------------------------------------------------------------------------

#[no_mangle]
pub unsafe extern "C" fn gettext(msgid: *const c_char) -> *mut c_char {
    msgid as *mut c_char
}

#[no_mangle]
pub unsafe extern "C" fn dgettext(_domain: *const c_char, msgid: *const c_char) -> *mut c_char {
    msgid as *mut c_char
}

#[no_mangle]
pub unsafe extern "C" fn dcgettext(
    _domain: *const c_char,
    msgid: *const c_char,
    _category: c_int,
) -> *mut c_char {
    msgid as *mut c_char
}

#[no_mangle]
pub unsafe extern "C" fn bindtextdomain(
    domain: *const c_char,
    _dir: *const c_char,
) -> *mut c_char {
    domain as *mut c_char
}

#[no_mangle]
pub unsafe extern "C" fn bind_textdomain_codeset(
    _domain: *const c_char,
    _codeset: *const c_char,
) -> *mut c_char {
    static mut UTF8: [c_char; 6] = [b'U' as c_char, b'T' as c_char, b'F' as c_char, b'-' as c_char, b'8' as c_char, 0];
    UTF8.as_mut_ptr()
}

// ---------------------------------------------------------------------------
// dynamic loading stubs (no shared objects in a freestanding kernel)
// ---------------------------------------------------------------------------

#[no_mangle]
pub unsafe extern "C" fn dlopen(_f: *const c_char, _m: c_int) -> *mut c_void {
    ptr::null_mut()
}

#[no_mangle]
pub unsafe extern "C" fn dlclose(_h: *mut c_void) -> c_int {
    0
}

#[no_mangle]
pub unsafe extern "C" fn dlsym(_h: *mut c_void, _n: *const c_char) -> *mut c_void {
    ptr::null_mut()
}

#[no_mangle]
pub unsafe extern "C" fn dlerror() -> *mut c_char {
    ptr::null_mut()
}

static mut TEXT_DOMAIN: [c_char; 1] = [0];

#[no_mangle]
pub unsafe extern "C" fn textdomain(domain: *const c_char) -> *mut c_char {
    if !domain.is_null() {
        return domain as *mut c_char;
    }
    TEXT_DOMAIN.as_mut_ptr()
}

// ---------------------------------------------------------------------------
// timezone data (musl: extern int daylight; extern long timezone;
// extern char *tzname[2])
// ---------------------------------------------------------------------------

#[no_mangle]
pub static mut daylight: c_int = 0;

#[no_mangle]
pub static mut timezone: i64 = 0;

static mut TZNAME0: [c_char; 4] = [b'U' as c_char, b'T' as c_char, b'C' as c_char, 0];
static mut TZNAME1: [c_char; 1] = [0];

/// musl: `extern char *tzname[2]`.
#[no_mangle]
pub static mut tzname: [*mut c_char; 2] = [ptr::null_mut(), ptr::null_mut()];

#[no_mangle]
pub unsafe extern "C" fn _tzname_init() {
    unsafe {
        tzname[0] = TZNAME0.as_mut_ptr();
        tzname[1] = TZNAME1.as_mut_ptr();
    }
}

static mut STRERR_BUF: [c_char; 64] = [0; 64];

#[no_mangle]
pub unsafe extern "C" fn strerror(err: c_int) -> *mut c_char {
    let msg: &[u8] = match err {
        1 => b"Operation not permitted\0",
        2 => b"No such file or directory\0",
        3 => b"No such process\0",
        4 => b"Interrupted system call\0",
        9 => b"Bad file descriptor\0",
        10 => b"No child processes\0",
        11 => b"Resource temporarily unavailable\0",
        12 => b"Cannot allocate memory\0",
        13 => b"Permission denied\0",
        16 => b"Device or resource busy\0",
        17 => b"File exists\0",
        21 => b"Is a directory\0",
        22 => b"Invalid argument\0",
        23 => b"Too many open files in system\0",
        25 => b"Inappropriate ioctl for device\0",
        38 => b"Function not implemented\0",
        _ => b"Unknown error\0",
    };
    for (i, b) in msg.iter().enumerate() {
        STRERR_BUF[i] = *b as c_char;
    }
    STRERR_BUF.as_mut_ptr()
}

static mut SIGERR_BUF: [c_char; 32] = [0; 32];

#[no_mangle]
pub unsafe extern "C" fn strsignal(sig: c_int) -> *mut c_char {
    let msg: &[u8] = match sig {
        1 => b"Hangup\0",
        2 => b"Interrupt\0",
        3 => b"Quit\0",
        6 => b"Aborted\0",
        8 => b"Floating point exception\0",
        9 => b"Killed\0",
        11 => b"Segmentation fault\0",
        13 => b"Broken pipe\0",
        14 => b"Alarm clock\0",
        15 => b"Terminated\0",
        _ => b"Unknown signal\0",
    };
    for (i, b) in msg.iter().enumerate() {
        SIGERR_BUF[i] = *b as c_char;
    }
    SIGERR_BUF.as_mut_ptr()
}

// ---------------------------------------------------------------------------
// math (via the libm crate)
// ---------------------------------------------------------------------------

#[no_mangle]
pub extern "C" fn atan2(y: f64, x: f64) -> f64 {
    libm::atan2(y, x)
}

#[no_mangle]
pub extern "C" fn sin(x: f64) -> f64 {
    libm::sin(x)
}

#[no_mangle]
pub extern "C" fn cos(x: f64) -> f64 {
    libm::cos(x)
}

#[no_mangle]
pub extern "C" fn tan(x: f64) -> f64 {
    libm::tan(x)
}

#[no_mangle]
pub extern "C" fn asin(x: f64) -> f64 {
    libm::asin(x)
}

#[no_mangle]
pub extern "C" fn acos(x: f64) -> f64 {
    libm::acos(x)
}

#[no_mangle]
pub extern "C" fn atan(x: f64) -> f64 {
    libm::atan(x)
}

#[no_mangle]
pub extern "C" fn sinh(x: f64) -> f64 {
    libm::sinh(x)
}

#[no_mangle]
pub extern "C" fn cosh(x: f64) -> f64 {
    libm::cosh(x)
}

#[no_mangle]
pub extern "C" fn tanh(x: f64) -> f64 {
    libm::tanh(x)
}

#[no_mangle]
pub extern "C" fn exp(x: f64) -> f64 {
    libm::exp(x)
}

#[no_mangle]
pub extern "C" fn exp2(x: f64) -> f64 {
    libm::exp2(x)
}

#[no_mangle]
pub extern "C" fn expm1(x: f64) -> f64 {
    libm::expm1(x)
}

#[no_mangle]
pub extern "C" fn log(x: f64) -> f64 {
    libm::log(x)
}

#[no_mangle]
pub extern "C" fn log10(x: f64) -> f64 {
    libm::log10(x)
}

#[no_mangle]
pub extern "C" fn log2(x: f64) -> f64 {
    libm::log2(x)
}

#[no_mangle]
pub extern "C" fn log1p(x: f64) -> f64 {
    libm::log1p(x)
}

#[no_mangle]
pub extern "C" fn pow(x: f64, y: f64) -> f64 {
    libm::pow(x, y)
}

#[no_mangle]
pub extern "C" fn sqrt(x: f64) -> f64 {
    libm::sqrt(x)
}

#[no_mangle]
pub extern "C" fn cbrt(x: f64) -> f64 {
    libm::cbrt(x)
}

#[no_mangle]
pub extern "C" fn floor(x: f64) -> f64 {
    libm::floor(x)
}

#[no_mangle]
pub extern "C" fn ceil(x: f64) -> f64 {
    libm::ceil(x)
}

#[no_mangle]
pub extern "C" fn trunc(x: f64) -> f64 {
    libm::trunc(x)
}

#[no_mangle]
pub extern "C" fn round(x: f64) -> f64 {
    libm::round(x)
}

#[no_mangle]
pub extern "C" fn fabs(x: f64) -> f64 {
    libm::fabs(x)
}

#[no_mangle]
pub extern "C" fn fmod(x: f64, y: f64) -> f64 {
    libm::fmod(x, y)
}

#[no_mangle]
pub extern "C" fn fmin(x: f64, y: f64) -> f64 {
    libm::fmin(x, y)
}

#[no_mangle]
pub extern "C" fn fmax(x: f64, y: f64) -> f64 {
    libm::fmax(x, y)
}

#[no_mangle]
pub extern "C" fn hypot(x: f64, y: f64) -> f64 {
    libm::hypot(x, y)
}

// NOTE on Rust/C ABI: for signatures whose FIRST parameter is a float and a
// later parameter is an integer/pointer, C's SysV ABI passes the
// integer/pointer in RDI (the first INTEGER register) while Rust's extern
// "C" reads it from RSI.  To make the two agree, the Rust declarations
// below list the integer/pointer parameter FIRST (the argument order the C
// side passes).  Verified against GCC -mabi=sysv and zig/clang linux
// targets.

#[no_mangle]
pub extern "C" fn ldexp(n: c_int, x: f64) -> f64 {
    libm::ldexp(x, n)
}

#[no_mangle]
pub extern "C" fn scalbn(n: c_int, x: f64) -> f64 {
    libm::scalbn(x, n)
}

#[no_mangle]
pub extern "C" fn copysign(x: f64, y: f64) -> f64 {
    libm::copysign(x, y)
}

#[no_mangle]
pub unsafe extern "C" fn frexp(exp: *mut c_int, x: f64) -> f64 {
    let (m, e) = libm::frexp(x);
    if !exp.is_null() {
        unsafe { *exp = e };
    }
    m
}

#[no_mangle]
pub unsafe extern "C" fn modf(iptr: *mut f64, x: f64) -> f64 {
    let (f, i) = libm::modf(x);
    if !iptr.is_null() {
        unsafe { *iptr = i };
    }
    f
}

// Extra libm exports needed by the `math` builtin module (M-module work).

#[no_mangle]
pub extern "C" fn lgamma(x: f64) -> f64 {
    libm::lgamma(x)
}

#[no_mangle]
pub extern "C" fn tgamma(x: f64) -> f64 {
    libm::tgamma(x)
}

#[no_mangle]
pub extern "C" fn erf(x: f64) -> f64 {
    libm::erf(x)
}

#[no_mangle]
pub extern "C" fn erfc(x: f64) -> f64 {
    libm::erfc(x)
}

#[no_mangle]
pub extern "C" fn asinh(x: f64) -> f64 {
    libm::asinh(x)
}

#[no_mangle]
pub extern "C" fn acosh(x: f64) -> f64 {
    libm::acosh(x)
}

#[no_mangle]
pub extern "C" fn atanh(x: f64) -> f64 {
    libm::atanh(x)
}

#[no_mangle]
pub extern "C" fn remainder(x: f64, y: f64) -> f64 {
    libm::remainder(x, y)
}

#[no_mangle]
pub extern "C" fn nextafter(x: f64, y: f64) -> f64 {
    libm::nextafter(x, y)
}

#[no_mangle]
pub extern "C" fn fma(x: f64, y: f64, z: f64) -> f64 {
    libm::fma(x, y, z)
}

#[no_mangle]
pub extern "C" fn fdim(x: f64, y: f64) -> f64 {
    libm::fdim(x, y)
}
