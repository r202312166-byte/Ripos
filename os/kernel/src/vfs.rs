//! In-memory filesystem (M4) + session-persistent RAM disk (M9.0).
//!
//! Serves CPython's open/read/stat/opendir from a ustar initramfs
//! embedded in the kernel image (the pure-Python stdlib from Lib/), plus
//! a few /dev pseudo-devices.  This makes import json and friends work.
//!
//! Since M9.0 the VFS also carries a **writable RAM disk mounted at /home**:
//! files and directories under /home live in heap-allocated buffers and
//! persist for the session (until reboot).  open() with write intent
//! (O_WRONLY/O_RDWR/O_CREAT/O_TRUNC/O_APPEND) is honoured there;
//! mkdir/rename/unlink/rmdir/truncate work on RAM-disk entries.
//! The initramfs itself stays read-only (its bytes live in the image).

use crate::libc::{set_errno, Stat, Timespec};
use alloc::format;
use core::ffi::{c_char, c_int, c_void};
use core::ptr;

const INITRAMFS: &[u8] = include_bytes!(concat!(env!("OUT_DIR"), "/initramfs.tar"));

const MAX_ENTRIES: usize = 4096;
const MAX_FDS: usize = 64;
/// Names are copied here at parse time (they must outlive the parse).
const NAME_ARENA: usize = MAX_ENTRIES * 64;
static mut NAME_BUF: [u8; NAME_ARENA] = [0; NAME_ARENA];
static mut NAME_USED: usize = 0;

const ENOENT: i32 = 2;
const EBADF: i32 = 9;
const EBUSY: i32 = 16;
const EEXIST: i32 = 17;
const ENOTDIR: i32 = 20;
const EISDIR: i32 = 21;
const EINVAL: i32 = 22;
const ENOSPC: i32 = 28;
const EROFS: i32 = 30;
const ENOTEMPTY: i32 = 39;

// musl open(2) flag values (x86-64).
const O_RDONLY: i32 = 0;
const O_WRONLY: i32 = 1;
const O_RDWR: i32 = 2;
const O_ACCMODE: i32 = 3;
const O_CREAT: i32 = 0o100;
const O_EXCL: i32 = 0o200;
const O_TRUNC: i32 = 0o1000;
const O_APPEND: i32 = 0o2000;

// musl struct dirent (x86-64).
#[repr(C)]
#[derive(Clone, Copy)]
pub struct Dirent {
    pub d_ino: u64,
    pub d_off: i64,
    pub d_reclen: u16,
    pub d_type: u8,
    pub d_name: [c_char; 256],
}

const DT_DIR: u8 = 4;
const DT_REG: u8 = 8;

#[derive(Clone, Copy)]
struct Entry {
    name: *const u8,
    name_len: usize,
    data: *const u8,
    data_len: usize,
    /// Heap capacity of data (RAM-disk files only).
    cap: usize,
    is_dir: bool,
    /// True for RAM-disk entries (heap-backed, writable).
    is_ram: bool,
    /// False once unlinked/removed; open fds keep working on the buffer.
    alive: bool,
}

static mut ENTRIES: [Entry; MAX_ENTRIES] = [Entry {
    name: ptr::null(),
    name_len: 0,
    data: ptr::null(),
    data_len: 0,
    cap: 0,
    is_dir: false,
    is_ram: false,
    alive: false,
}; MAX_ENTRIES];
static mut ENTRY_COUNT: usize = 0;

#[derive(Clone, Copy)]
enum Fd {
    Free,
    Stdin,
    Serial,
    Urandom,
    Null,
    Zero,
    File {
        entry: usize,
        pos: usize,
    },
}

static mut FDS: [Fd; MAX_FDS] = [Fd::Free; MAX_FDS];

struct Dir {
    children: alloc::vec::Vec<(alloc::string::String, u8)>,
    idx: usize,
    dirent: Dirent,
}

// ---------------------------------------------------------------------------
// initramfs parsing
// ---------------------------------------------------------------------------

fn octal(h: &[u8], off: usize, len: usize) -> usize {
    let mut v = 0usize;
    for &b in &h[off..off + len] {
        if b == 0 || b == b' ' {
            break;
        }
        if (b'0'..=b'7').contains(&b) {
            v = v * 8 + (b - b'0') as usize;
        }
    }
    v
}

/// Normalize a stored/query path: drop "./" and leading "/" prefixes and
/// trailing "/" so "json/__init__.py", "./json/__init__.py", "/json/__init__.py"
/// all match the same entry.
fn normalize(name: &[u8]) -> &[u8] {
    let mut s = name;
    while s.starts_with(b"./") {
        s = &s[2..];
    }
    while s.starts_with(b"/") {
        s = &s[1..];
    }
    while s.ends_with(b"/") {
        s = &s[..s.len() - 1];
    }
    s
}

unsafe fn add_entry(name: &[u8], data: *const u8, data_len: usize, is_dir: bool) {
    unsafe {
        if ENTRY_COUNT >= MAX_ENTRIES {
            return;
        }
        // Copy the normalized name into the arena: the caller's slice may
        // point into a temporary buffer.
        let n = normalize(name);
        let nlen = n.len().min(63);
        let off = NAME_USED;
        NAME_USED += nlen + 1;
        if NAME_USED > NAME_ARENA {
            return;
        }
        NAME_BUF[off..off + nlen].copy_from_slice(&n[..nlen]);
        NAME_BUF[off + nlen] = 0;
        ENTRIES[ENTRY_COUNT] = Entry {
            name: NAME_BUF.as_ptr().add(off),
            name_len: nlen,
            data,
            data_len,
            cap: 0,
            is_dir,
            is_ram: false,
            alive: true,
        };
        ENTRY_COUNT += 1;
    }
}

/// Create a new writable RAM-disk entry (heap-backed).  Returns its index.
unsafe fn create_ram_entry(name: &[u8], is_dir: bool) -> Option<usize> {
    unsafe {
        if ENTRY_COUNT >= MAX_ENTRIES {
            return None;
        }
        let n = normalize(name);
        let nlen = n.len().min(63);
        let off = NAME_USED;
        NAME_USED += nlen + 1;
        if NAME_USED > NAME_ARENA {
            return None;
        }
        NAME_BUF[off..off + nlen].copy_from_slice(&n[..nlen]);
        NAME_BUF[off + nlen] = 0;
        ENTRIES[ENTRY_COUNT] = Entry {
            name: NAME_BUF.as_ptr().add(off),
            name_len: nlen,
            data: ptr::null_mut(),
            data_len: 0,
            cap: 0,
            is_dir,
            is_ram: true,
            alive: true,
        };
        ENTRY_COUNT += 1;
        Some(ENTRY_COUNT - 1)
    }
}

unsafe fn parse_tar() {
    unsafe {
        let data = INITRAMFS;
        let mut off = 0usize;
        while off + 512 <= data.len() {
            let h = &data[off..off + 512];
            if h.iter().all(|&b| b == 0) {
                break; // end-of-archive marker
            }
            let size = octal(h, 124, 12);
            let typeflag = h[156];
            let padded = (size + 511) & !511;
            let data_off = off + 512;
            if typeflag == b'0' || typeflag == 0 {
                // Regular file.  ustar may split long names into
                // prefix (345..500) + "/" + name (0..100).
                let name_field = &h[0..100];
                let name_end = name_field.iter().position(|&b| b == 0).unwrap_or(100);
                let mut name: alloc::vec::Vec<u8> = alloc::vec::Vec::new();
                let prefix_field = &h[345..500];
                let prefix_end = prefix_field.iter().position(|&b| b == 0).unwrap_or(155);
                if prefix_end > 0 {
                    name.extend_from_slice(&prefix_field[..prefix_end]);
                    name.push(b'/');
                }
                name.extend_from_slice(&name_field[..name_end]);
                let n = normalize(&name);
                add_entry(n, data.as_ptr().add(data_off), size, false);
            } else if typeflag == b'5' {
                let name_field = &h[0..100];
                let name_end = name_field.iter().position(|&b| b == 0).unwrap_or(100);
                let n = normalize(&name_field[..name_end]);
                add_entry(n, ptr::null(), 0, true);
            }
            // 'x'/'g' (pax) and others: skip payload; ustar generation avoids them.
            off = data_off + padded;
        }
    }
}

// ---------------------------------------------------------------------------
// lookups
// ---------------------------------------------------------------------------

fn slice_eq(a: &[u8], b: &[u8]) -> bool {
    a.len() == b.len() && {
        let mut i = 0;
        while i < a.len() {
            if a[i] != b[i] {
                return false;
            }
            i += 1;
        }
        true
    }
}

/// Entry index for a normalized path, or None.
fn lookup(path: &[u8]) -> Option<usize> {
    let p = normalize(path);
    unsafe {
        for i in 0..ENTRY_COUNT {
            let e = &ENTRIES[i];
            if !e.alive {
                continue;
            }
            let name = core::slice::from_raw_parts(e.name, e.name_len);
            if slice_eq(name, p) {
                return Some(i);
            }
        }
    }
    None
}

fn entry_is_dir(path: &[u8]) -> bool {
    let p = normalize(path);
    if let Some(i) = lookup(p) {
        unsafe { return ENTRIES[i].is_dir };
    }
    // A directory may exist implicitly (no explicit tar dir entry): some
    // file lives under it.
    unsafe {
        for i in 0..ENTRY_COUNT {
            let e = &ENTRIES[i];
            if !e.alive {
                continue;
            }
            let name = core::slice::from_raw_parts(e.name, e.name_len);
            if name.len() > p.len()
                && name[p.len()] == b'/'
                && slice_eq(&name[..p.len()], p)
            {
                return true;
            }
        }
    }
    false
}

// ---------------------------------------------------------------------------
// RAM disk (/home)
// ---------------------------------------------------------------------------

/// True if a normalized path lives on the writable RAM disk.
fn is_ram_path(p: &[u8]) -> bool {
    let p = normalize(p);
    p == b"home" || p.starts_with(b"home/")
}

/// The parent of a normalized path must be a directory.
fn parent_is_dir(p: &[u8]) -> bool {
    let p = normalize(p);
    if let Some(slash) = p.iter().rposition(|&b| b == b'/') {
        entry_is_dir(&p[..slash])
    } else {
        // Top-level: the only writable top-level name is the /home mount
        // itself, whose parent is the root directory.
        p == b"home"
    }
}

unsafe fn ram_alloc_bytes(n: usize) -> *mut u8 {
    if n == 0 {
        return ptr::null_mut();
    }
    let layout = alloc::alloc::Layout::from_size_align(n, 8).unwrap();
    unsafe { alloc::alloc::alloc(layout) }
}

unsafe fn ram_free_bytes(p: *mut u8, cap: usize) {
    if p.is_null() || cap == 0 {
        return;
    }
    let layout = alloc::alloc::Layout::from_size_align(cap, 8).unwrap();
    unsafe { alloc::alloc::dealloc(p, layout) };
}

/// Grow the heap buffer of entry eidx so it can hold need bytes.
unsafe fn ram_grow(eidx: usize, need: usize) -> bool {
    unsafe {
        if ENTRIES[eidx].cap >= need {
            return true;
        }
        let newcap = need.max(ENTRIES[eidx].cap * 2).max(64);
        let np = ram_alloc_bytes(newcap);
        if np.is_null() {
            return false;
        }
        let old = ENTRIES[eidx].data;
        let oldlen = ENTRIES[eidx].data_len;
        if oldlen > 0 && !old.is_null() {
            ptr::copy_nonoverlapping(old, np, oldlen);
        }
        ram_free_bytes(old as *mut u8, ENTRIES[eidx].cap);
        ENTRIES[eidx].data = np;
        ENTRIES[eidx].cap = newcap;
        true
    }
}

/// Point an entry's name at a fresh arena slot (used by rename).
unsafe fn set_entry_name(eidx: usize, name: &[u8]) -> bool {
    unsafe {
        let n = normalize(name);
        let nlen = n.len().min(63);
        let off = NAME_USED;
        NAME_USED += nlen + 1;
        if NAME_USED > NAME_ARENA {
            return false;
        }
        NAME_BUF[off..off + nlen].copy_from_slice(&n[..nlen]);
        NAME_BUF[off + nlen] = 0;
        ENTRIES[eidx].name = NAME_BUF.as_ptr().add(off);
        ENTRIES[eidx].name_len = nlen;
        true
    }
}

/// Number of alive entries directly under a normalized directory path.
fn dir_child_count(p: &[u8]) -> usize {
    let p = normalize(p);
    let prefix = if p.is_empty() {
        alloc::string::String::new()
    } else {
        format!("{}/", unsafe { alloc::string::String::from_utf8_unchecked(p.to_vec()) })
    };
    let mut n = 0usize;
    unsafe {
        for i in 0..ENTRY_COUNT {
            let e = &ENTRIES[i];
            if !e.alive {
                continue;
            }
            let name = core::slice::from_raw_parts(e.name, e.name_len);
            if name.len() > prefix.len() && name.starts_with(prefix.as_bytes()) {
                n += 1;
            }
        }
    }
    n
}

#[no_mangle]
pub unsafe extern "C" fn kern_mkdir(path: *const c_char, _mode: u32) -> c_int {
    unsafe {
        let pn = normalize(cstr(path));
        if !is_ram_path(pn) {
            set_errno(EROFS);
            return -1;
        }
        if lookup(pn).is_some() {
            set_errno(EEXIST);
            return -1;
        }
        if !parent_is_dir(pn) {
            set_errno(ENOTDIR);
            return -1;
        }
        if create_ram_entry(pn, true).is_none() {
            set_errno(ENOSPC);
            return -1;
        }
        0
    }
}

#[no_mangle]
pub unsafe extern "C" fn kern_unlink(path: *const c_char) -> c_int {
    unsafe {
        let pn = normalize(cstr(path));
        if !is_ram_path(pn) || pn == b"home" {
            set_errno(EROFS);
            return -1;
        }
        let i = match lookup(pn) {
            Some(i) => i,
            None => {
                set_errno(ENOENT);
                return -1;
            }
        };
        let e = &ENTRIES[i];
        if e.is_dir {
            set_errno(EISDIR);
            return -1;
        }
        if !e.is_ram {
            set_errno(EROFS);
            return -1;
        }
        ENTRIES[i].alive = false;
        0
    }
}

#[no_mangle]
pub unsafe extern "C" fn kern_rmdir(path: *const c_char) -> c_int {
    unsafe {
        let pn = normalize(cstr(path));
        if !is_ram_path(pn) || pn == b"home" {
            set_errno(EROFS);
            return -1;
        }
        let i = match lookup(pn) {
            Some(i) => i,
            None => {
                set_errno(ENOENT);
                return -1;
            }
        };
        let e = &ENTRIES[i];
        if !e.is_dir {
            set_errno(ENOTDIR);
            return -1;
        }
        if dir_child_count(pn) > 0 {
            set_errno(ENOTEMPTY);
            return -1;
        }
        ENTRIES[i].alive = false;
        0
    }
}

#[no_mangle]
pub unsafe extern "C" fn kern_rename(old: *const c_char, new: *const c_char) -> c_int {
    unsafe {
        let on = normalize(cstr(old));
        let nn = normalize(cstr(new));
        if !is_ram_path(on) || !is_ram_path(nn) || on == b"home" || nn == b"home" {
            set_errno(EROFS);
            return -1;
        }
        let i = match lookup(on) {
            Some(i) => i,
            None => {
                set_errno(ENOENT);
                return -1;
            }
        };
        if ENTRIES[i].is_dir {
            // Directory renames would require rewriting children's names
            // (the name arena is append-only): unsupported for now.
            set_errno(EINVAL);
            return -1;
        }
        if lookup(nn).is_some() {
            set_errno(EEXIST);
            return -1;
        }
        if !parent_is_dir(nn) {
            set_errno(ENOTDIR);
            return -1;
        }
        if !set_entry_name(i, nn) {
            set_errno(ENOSPC);
            return -1;
        }
        0
    }
}

#[no_mangle]
pub unsafe extern "C" fn kern_ftruncate(fd: c_int, len: i64) -> c_int {
    unsafe {
        if fd < 3 || fd as usize >= MAX_FDS {
            set_errno(EBADF);
            return -1;
        }
        let entry = match FDS[fd as usize] {
            Fd::File { entry, .. } => entry,
            _ => {
                set_errno(EINVAL);
                return -1;
            }
        };
        if !ENTRIES[entry].is_ram {
            set_errno(EROFS);
            return -1;
        }
        if len < 0 {
            set_errno(EINVAL);
            return -1;
        }
        let nlen = len as usize;
        if nlen > ENTRIES[entry].cap {
            if !ram_grow(entry, nlen) {
                set_errno(ENOSPC);
                return -1;
            }
        }
        if nlen > ENTRIES[entry].data_len {
            let e = &mut ENTRIES[entry];
            let base = e.data as *mut u8;
            if !base.is_null() {
                ptr::write_bytes(base.add(e.data_len), 0, nlen - e.data_len);
            }
            e.data_len = nlen;
        } else {
            ENTRIES[entry].data_len = nlen;
        }
        0
    }
}

#[no_mangle]
pub unsafe extern "C" fn kern_truncate(path: *const c_char, len: i64) -> c_int {
    unsafe {
        let pn = normalize(cstr(path));
        let i = match lookup(pn) {
            Some(i) => i,
            None => {
                set_errno(ENOENT);
                return -1;
            }
        };
        if !ENTRIES[i].is_ram || ENTRIES[i].is_dir {
            set_errno(EROFS);
            return -1;
        }
        let fd = alloc_fd(Fd::File { entry: i, pos: 0 });
        let r = kern_ftruncate(fd, len);
        if fd >= 0 {
            FDS[fd as usize] = Fd::Free;
        }
        r
    }
}

// ---------------------------------------------------------------------------
// fd table
// ---------------------------------------------------------------------------

fn alloc_fd(kind: Fd) -> c_int {
    unsafe {
        for i in 3..MAX_FDS {
            if matches!(FDS[i], Fd::Free) {
                FDS[i] = kind;
                return i as c_int;
            }
        }
    }
    -1
}

unsafe fn fill_stat(st: *mut Stat, mode: u32, size: i64) {
    unsafe {
        let mono_ns = crate::time::monotonic_ns();
        let ts = Timespec {
            tv_sec: 1_700_000_000 + (mono_ns / 1_000_000_000) as i64,
            tv_nsec: (mono_ns % 1_000_000_000) as i64,
        };
        (*st).st_dev = 0;
        (*st).st_ino = 0;
        (*st).st_nlink = 1;
        (*st).st_mode = mode;
        (*st).st_uid = 0;
        (*st).st_gid = 0;
        (*st).__pad0 = 0;
        (*st).st_rdev = 0;
        (*st).st_size = size;
        (*st).st_blksize = 4096;
        (*st).st_blocks = 0;
        (*st).st_atim = ts;
        (*st).st_mtim = ts;
        (*st).st_ctim = ts;
        (*st).__unused = [0; 3];
    }
}

fn cstr_len(s: *const c_char) -> usize {
    let mut n = 0;
    while unsafe { *s.add(n) } != 0 {
        n += 1;
    }
    n
}

fn cstr(s: *const c_char) -> &'static [u8] {
    let len = cstr_len(s);
    unsafe { core::slice::from_raw_parts(s as *const u8, len) }
}

const S_IFCHR: u32 = 0o020000;
const S_IFREG: u32 = 0o100000;
const S_IRUSR: u32 = 0o400;
const S_IWUSR: u32 = 0o200;

fn dev_stat(path: &[u8]) -> Option<u32> {
    let mode = match path {
        b"/dev/urandom" | b"/dev/serial" | b"/dev/stdout" | b"/dev/console" | b"/dev/null" | b"/dev/zero" => {
            S_IFCHR | S_IRUSR | S_IWUSR
        }
        _ => return None,
    };
    Some(mode)
}

// ---------------------------------------------------------------------------
// exported libc surface
// ---------------------------------------------------------------------------

/// Open a path with open(2) flags (used by the C shim's variadic open).
/// Write intent on the RAM disk creates/truncates/appends as requested;
/// the initramfs stays read-only.
#[no_mangle]
pub unsafe extern "C" fn kern_open(path: *const c_char, flags: c_int) -> c_int {
    unsafe {
        let p = cstr(path);
        match p {
            b"/dev/urandom" => return alloc_fd(Fd::Urandom),
            b"/dev/serial" | b"/dev/stdout" | b"/dev/console" => return alloc_fd(Fd::Serial),
            b"/dev/null" => return alloc_fd(Fd::Null),
            b"/dev/zero" => return alloc_fd(Fd::Zero),
            _ => {}
        }
        let pn = normalize(p);
        let writing = (flags & O_ACCMODE) != O_RDONLY;
        if let Some(i) = lookup(pn) {
            let e = &ENTRIES[i];
            if e.is_dir {
                set_errno(EISDIR);
                return -1;
            }
            if writing && !e.is_ram {
                set_errno(EROFS);
                return -1;
            }
            if writing && e.is_ram && (flags & O_TRUNC) != 0 {
                ENTRIES[i].data_len = 0;
            }
            let start = if writing && e.is_ram && (flags & O_APPEND) != 0 {
                ENTRIES[i].data_len
            } else {
                0
            };
            return alloc_fd(Fd::File { entry: i, pos: start });
        }
        // Not found: writable RAM-disk paths may be created with O_CREAT.
        if writing && is_ram_path(pn) && (flags & O_CREAT) != 0 {
            if pn == b"home" {
                set_errno(EISDIR);
                return -1;
            }
            if !parent_is_dir(pn) {
                set_errno(ENOTDIR);
                return -1;
            }
            let i = match create_ram_entry(pn, false) {
                Some(i) => i,
                None => {
                    set_errno(ENOSPC);
                    return -1;
                }
            };
            return alloc_fd(Fd::File { entry: i, pos: 0 });
        }
        set_errno(ENOENT);
        -1
    }
}

#[no_mangle]
pub unsafe extern "C" fn read(fd: c_int, buf: *mut c_void, count: usize) -> isize {
    unsafe {
        match fd {
            0 => 0, // stdin: EOF
            1 | 2 => {
                set_errno(EBADF);
                -1
            }
            _ => match &mut FDS[fd as usize] {
                Fd::Free => {
                    set_errno(EBADF);
                    -1
                }
                Fd::Urandom => {
                    let mut state = crate::time::rdtsc();
                    let n = count.min(64);
                    let out = buf as *mut u8;
                    for i in 0..n {
                        state ^= state >> 12;
                        state ^= state << 25;
                        state ^= state >> 27;
                        let v = state.wrapping_mul(0x2545F4914F6CDD1D);
                        *out.add(i) = (v >> 56) as u8;
                    }
                    n as isize
                }
                Fd::Zero => {
                    let n = count.min(4096);
                    ptr::write_bytes(buf, 0, n);
                    n as isize
                }
                Fd::Serial | Fd::Stdin | Fd::Null => {
                    set_errno(EBADF);
                    -1
                }
                Fd::File { entry, pos } => {
                    let e = &ENTRIES[*entry];
                    let remaining = e.data_len.saturating_sub(*pos);
                    let n = count.min(remaining);
                    if n > 0 && !e.data.is_null() {
                        ptr::copy_nonoverlapping(e.data.add(*pos), buf as *mut u8, n);
                    }
                    *pos += n;
                    n as isize
                }
            },
        }
    }
}

#[no_mangle]
pub unsafe extern "C" fn write(fd: c_int, buf: *const c_void, count: usize) -> isize {
    unsafe {
        match fd {
            1 | 2 => {
                crate::serial::write_bytes(core::slice::from_raw_parts(buf as *const u8, count));
                count as isize
            }
            _ => match &mut FDS[fd as usize] {
                Fd::Free => {
                    set_errno(EBADF);
                    -1
                }
                Fd::Serial => {
                    crate::serial::write_bytes(core::slice::from_raw_parts(buf as *const u8, count));
                    count as isize
                }
                Fd::Null => count as isize,
                Fd::File { entry, pos } => {
                    let e = &ENTRIES[*entry];
                    if !e.is_ram {
                        set_errno(EROFS);
                        return -1;
                    }
                    let need = *pos + count;
                    if !ram_grow(*entry, need) {
                        set_errno(ENOSPC);
                        return -1;
                    }
                    let e = &mut ENTRIES[*entry];
                    let base = e.data as *mut u8;
                    if !base.is_null() {
                        if *pos > e.data_len {
                            ptr::write_bytes(base.add(e.data_len), 0, *pos - e.data_len);
                        }
                        ptr::copy_nonoverlapping(buf as *const u8, base.add(*pos), count);
                    }
                    if need > e.data_len {
                        e.data_len = need;
                    }
                    *pos += count;
                    count as isize
                }
                Fd::Urandom | Fd::Zero => {
                    set_errno(EROFS);
                    -1
                }
                Fd::Stdin => {
                    set_errno(EBADF);
                    -1
                }
            },
        }
    }
}

#[no_mangle]
pub unsafe extern "C" fn close(fd: c_int) -> c_int {
    if fd < 3 {
        return 0;
    }
    unsafe {
        if fd as usize >= MAX_FDS {
            set_errno(EBADF);
            return -1;
        }
        FDS[fd as usize] = Fd::Free;
    }
    0
}

#[no_mangle]
pub unsafe extern "C" fn lseek(fd: c_int, off: i64, whence: c_int) -> i64 {
    unsafe {
        if let Fd::File { entry, pos } = &mut FDS[fd as usize] {
            let size = ENTRIES[*entry].data_len;
            let base = match whence {
                0 => 0i64,                 // SEEK_SET
                1 => *pos as i64,          // SEEK_CUR
                2 => size as i64,          // SEEK_END
                _ => {
                    set_errno(EINVAL);
                    return -1;
                }
            };
            let newp = base + off;
            if newp < 0 {
                set_errno(EINVAL);
                return -1;
            }
            *pos = (newp as usize).min(size);
            *pos as i64
        } else {
            set_errno(EINVAL);
            -1
        }
    }
}

#[no_mangle]
pub unsafe extern "C" fn fstat(fd: c_int, st: *mut Stat) -> c_int {
    unsafe {
        match fd {
            0 | 1 | 2 => {
                fill_stat(st, S_IFCHR | S_IRUSR | S_IWUSR, 0);
                0
            }
            _ => match &FDS[fd as usize] {
                Fd::Free => {
                    set_errno(EBADF);
                    -1
                }
                Fd::Urandom | Fd::Serial | Fd::Null | Fd::Zero => {
                    fill_stat(st, S_IFCHR | S_IRUSR | S_IWUSR, 0);
                    0
                }
                Fd::File { entry, .. } => {
                    let e = &ENTRIES[*entry];
                    let mode = if e.is_dir {
                        0o040000 | 0o755
                    } else if e.is_ram {
                        S_IFREG | 0o644
                    } else {
                        S_IFREG | 0o444
                    };
                    fill_stat(st, mode, e.data_len as i64);
                    0
                }
                Fd::Stdin => {
                    set_errno(EBADF);
                    -1
                }
            },
        }
    }
}

#[no_mangle]
pub unsafe extern "C" fn stat(path: *const c_char, st: *mut Stat) -> c_int {
    unsafe {
        let p = cstr(path);
        if let Some(mode) = dev_stat(p) {
            fill_stat(st, mode, 0);
            return 0;
        }
        // The root directory always exists.
        if p.is_empty() || p == b"/" {
            fill_stat(st, 0o040000 | 0o755, 0);
            return 0;
        }
        if let Some(i) = lookup(p) {
            let e = &ENTRIES[i];
            if e.is_dir {
                fill_stat(st, 0o040000 | 0o755, 0);
            } else if e.is_ram {
                fill_stat(st, S_IFREG | 0o644, e.data_len as i64);
            } else {
                fill_stat(st, S_IFREG | 0o444, e.data_len as i64);
            }
            return 0;
        }
        set_errno(ENOENT);
        -1
    }
}

#[no_mangle]
pub unsafe extern "C" fn lstat(path: *const c_char, st: *mut Stat) -> c_int {
    stat(path, st)
}

#[no_mangle]
pub unsafe extern "C" fn access(path: *const c_char, _mode: c_int) -> c_int {
    unsafe {
        let p = cstr(path);
        if dev_stat(p).is_some() || p.is_empty() || p == b"/" || lookup(p).is_some() || entry_is_dir(p)
        {
            return 0;
        }
        set_errno(ENOENT);
        -1
    }
}

// -- directories ----------------------------------------------------------

#[no_mangle]
pub unsafe extern "C" fn opendir(path: *const c_char) -> *mut c_void {
    unsafe {
        let p = cstr(path);
        let base = if p == b"/" || p.is_empty() {
            alloc::string::String::new()
        } else {
            alloc::string::String::from_utf8_unchecked(normalize(p).to_vec())
        };
        if base.is_empty() {
            // root always exists
        } else if !entry_is_dir(base.as_bytes()) {
            set_errno(ENOENT);
            return ptr::null_mut();
        }
        // Collect first-level children, deduped.
        let mut children: alloc::vec::Vec<(alloc::string::String, u8)> = alloc::vec::Vec::new();
        let prefix = if base.is_empty() {
            alloc::string::String::new()
        } else {
            format!("{base}/")
        };
        let mut seen: alloc::vec::Vec<alloc::string::String> = alloc::vec::Vec::new();
        for i in 0..ENTRY_COUNT {
            let e = &ENTRIES[i];
            if !e.alive {
                continue;
            }
            let name = core::slice::from_raw_parts(e.name, e.name_len);
            if !name.starts_with(prefix.as_bytes()) {
                continue;
            }
            let rest = &name[prefix.len()..];
            if rest.is_empty() {
                continue; // the dir itself
            }
            let slash = rest.iter().position(|&b| b == b'/');
            let (child, is_dir) = match slash {
                Some(j) => (rest[..j].to_vec(), true),
                None => (rest.to_vec(), false),
            };
            let child_s = alloc::string::String::from_utf8_unchecked(child);
            if !seen.contains(&child_s) {
                seen.push(child_s.clone());
                children.push((child_s, if is_dir { DT_DIR } else { DT_REG }));
            }
        }
        let dir = alloc::boxed::Box::new(Dir {
            children,
            idx: 0,
            dirent: Dirent {
                d_ino: 0,
                d_off: 0,
                d_reclen: 0,
                d_type: 0,
                d_name: [0; 256],
            },
        });
        alloc::boxed::Box::into_raw(dir) as *mut c_void
    }
}

#[no_mangle]
pub unsafe extern "C" fn readdir(dirp: *mut c_void) -> *mut c_void {
    unsafe {
        if dirp.is_null() {
            return ptr::null_mut();
        }
        let dir = &mut *(dirp as *mut Dir);
        if dir.idx >= dir.children.len() {
            return ptr::null_mut();
        }
        let (name, dtype) = &dir.children[dir.idx];
        dir.idx += 1;
        let name_bytes = name.as_bytes();
        dir.dirent.d_ino = 1;
        dir.dirent.d_off = dir.idx as i64;
        dir.dirent.d_reclen = 0;
        dir.dirent.d_type = *dtype;
        dir.dirent.d_name.fill(0);
        let n = name_bytes.len().min(255);
        for i in 0..n {
            dir.dirent.d_name[i] = name_bytes[i] as c_char;
        }
        &mut dir.dirent as *mut Dirent as *mut c_void
    }
}

#[no_mangle]
pub unsafe extern "C" fn closedir(dirp: *mut c_void) -> c_int {
    if dirp.is_null() {
        return 0;
    }
    unsafe {
        drop(alloc::boxed::Box::from_raw(dirp as *mut Dir));
    }
    0
}

/// Initialize the filesystem: set up the console fds, parse the
/// initramfs and mount the writable /home RAM disk.  Called once at boot
/// before Python starts.
pub unsafe fn init() {
    unsafe {
        FDS[0] = Fd::Stdin;
        FDS[1] = Fd::Serial;
        FDS[2] = Fd::Serial;
        parse_tar();
        add_entry(b"home", ptr::null(), 0, true);
        crate::serial::write_str("vfs: initramfs parsed, ");
        crate::serial::write_u64(ENTRY_COUNT as u64);
        crate::serial::write_str(" entries, /home RAM disk mounted\n");
    }
}