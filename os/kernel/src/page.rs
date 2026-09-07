//! Minimal page-table extension: identity-map the C heap's physical range
//! with 2 MiB huge pages.
//!
//! Legacy: the vendor CPython was built with mingw, where `uintptr_t`/
//! `unsigned long` is only 4 bytes, and several CPython structures
//! (obmalloc arena addresses, GC link fields) store pointers through
//! `uintptr_t`, truncating every address above 4 GiB to its low 32 bits.
//! The LP64 build has no truncated pointers, but keeping the heap range
//! explicitly identity-mapped is harmless and guarantees the heap is
//! reachable even if the bootloader's own identity map is incomplete.
//!
//!   * [96 MiB, 192 MiB) -> the C heap's physical range (0x6000000..
//!     0xC000000)
//!
//! The kernel image itself lives below 1 GiB and is already mapped
//! identity, so no other range is affected.

use alloc::format;

/// Read the root of the active 4-level page tables.
pub fn read_cr3() -> u64 {
    let mut cr3: u64;
    unsafe {
        core::arch::asm!("mov {0}, cr3", out(reg) cr3, options(nomem, nostack));
    }
    cr3
}

/// Extend the bootloader framebuffer mapping so runtime Bochs-VBE mode
/// changes (res 1920x1080) have physical backing.  The bootloader maps
/// only the boot mode bytes (byte_len); we translate the existing
/// mapping to its physical base and add 2 MiB pages covering
/// [virt, virt + target_len).  Returns true on success; a failure is
/// harmless (the OS keeps the boot resolution).
pub unsafe fn extend_framebuffer_map(
    phys_offset: u64,
    virt: u64,
    byte_len: u64,
    target_len: u64,
) -> bool {
    unsafe {
        if target_len <= byte_len {
            return true;
        }
        const PS: u64 = 0x20_0000; // 2 MiB
        let mask = 0x000F_FFFF_FFFF_F000u64;
        let idx = |shift: u32, v: u64| ((v >> shift) & 0x1FF) as usize;
        let cr3 = read_cr3();
        let phys = walk(phys_offset, cr3, virt);
        if phys == 0 {
            crate::serial::write_str("fbmap: cannot resolve framebuffer phys\n");
            return false;
        }
        let pbase = phys & !(PS - 1);
        let vbase = virt & !(PS - 1);
        let slots = (target_len + PS - 1) / PS;
        let pml4 = (phys_offset + (cr3 & mask)) as *mut u64;
        let mut ok = true;
        for i in 0..slots {
            let v = vbase + i * PS;
            let e4 = unsafe { *pml4.add(idx(39, v)) };
            if e4 & 1 == 0 {
                ok = false;
                break;
            }
            let pdpt = (phys_offset + (e4 & mask)) as *const u64;
            let e3 = unsafe { *pdpt.add(idx(30, v)) };
            if e3 & 1 == 0 {
                ok = false;
                break;
            }
            if e3 & (1 << 7) != 0 {
                // A 1 GiB page already covers this slot: nothing to add.
                continue;
            }
            let pd = (phys_offset + (e3 & mask)) as *mut u64;
            unsafe {
                *pd.add(idx(21, v)) = (pbase + i * PS) | 0x83;
            }
        }
        if ok {
            for i in 0..slots {
                let v = vbase + i * PS;
                unsafe {
                    core::arch::asm!("invlpg [{0}]", in(reg) v, options(nostack, preserves_flags));
                }
            }
            crate::serial::write_str(&format!("fbmap: framebuffer mapping extended to {} bytes (phys {:x})\n", target_len, pbase));
        }
        ok
    }
}

/// Identity-map a physical MMIO range [phys, phys + len) with 2 MiB huge
/// pages, creating the PML4/PDPT/PD entries as needed (the NIC's MMIO BAR
/// lives at ~0xfebc0000, above the low-1 GiB identity map).  Page-table
/// pages come from the Rust heap (their physical address = pointer -
/// phys_offset).  Returns true on success.
pub unsafe fn map_identity_mmio(phys_offset: u64, phys: u64, len: u64) -> bool {
    unsafe {
        const PS: u64 = 0x20_0000; // 2 MiB
        const MASK: u64 = 0x000F_FFFF_FFFF_F000;
        let cr3 = read_cr3();
        let pml4 = (phys_offset + (cr3 & MASK)) as *mut u64;
        let start = phys & !(PS - 1);
        let slots = ((len + PS - 1) / PS) as usize;
        for i in 0..slots {
            let v = start + (i as u64) * PS;
            let l4 = ((v >> 39) & 0x1FF) as usize;
            let l3 = ((v >> 30) & 0x1FF) as usize;
            let l2 = ((v >> 21) & 0x1FF) as usize;
            let mut e4 = unsafe { *pml4.add(l4) };
            if e4 & 1 == 0 {
                let Some(p) = alloc_page_phys(phys_offset) else { return false };
                unsafe { *pml4.add(l4) = p | 0x3 };
                e4 = p | 0x3;
            }
            let pdpt = (phys_offset + (e4 & MASK)) as *mut u64;
            let mut e3 = unsafe { *pdpt.add(l3) };
            if e3 & 1 == 0 {
                let Some(p) = alloc_page_phys(phys_offset) else { return false };
                unsafe { *pdpt.add(l3) = p | 0x3 };
                e3 = p | 0x3;
            }
            if e3 & (1 << 7) != 0 {
                // A 1 GiB page already covers this slot: nothing to add.
                continue;
            }
            let pd = (phys_offset + (e3 & MASK)) as *mut u64;
            unsafe {
                *pd.add(l2) = v | 0x83; // present | rw | huge
                core::arch::asm!("invlpg [{0}]", in(reg) v, options(nostack, preserves_flags));
            }
            crate::serial::write_str(&alloc::format!(
                "mmio: v={:#x} pml4[{}]={:#x} pdpt[{}]={:#x} pd[{}]={:#x}\n",
                v, l4, e4, l3, e3, l2, unsafe { *pd.add(l2) }
            ));
        }
        true
    }
}

/// Allocate one zeroed 4 KiB page from the Rust heap and return its
/// physical address (the heap is mapped at phys_offset + physical).
unsafe fn alloc_page_phys(phys_offset: u64) -> Option<u64> {
    unsafe {
        // 8 KiB so the 4 KiB-aligned sub-page is fully ours (the allocator
        // only guarantees 8/16-byte alignment; page-table pages need 4K).
        let mut v: alloc::vec::Vec<u64> = alloc::vec::Vec::new();
        v.resize(1024, 0u64);
        let base = v.as_mut_ptr() as u64;
        core::mem::forget(v);
        if base == 0 {
            return None;
        }
        let aligned = (base + 4095) & !4095u64;
        Some(aligned - phys_offset)
    }
}

/// Map the truncated-pointer alias ranges using the PD page already
/// referenced by PML4[0] -> PDPT[0].
pub unsafe fn map_low_1g_identity(phys_offset: u64) {
    unsafe {
        let mut cr3: u64;
        core::arch::asm!("mov {0}, cr3", out(reg) cr3, options(nomem, nostack));
        let pml4 = (phys_offset + (cr3 & 0x000F_FFFF_FFFF_F000)) as *mut u64;
        let pml4_0 = *pml4;
        // The kernel runs below 1 GiB, so PML4[0] must be present.
        if pml4_0 & 1 == 0 {
            crate::serial::write_str("page: PML4[0] absent, cannot map low 1G\n");
            return;
        }
        let pdpt = (phys_offset + (pml4_0 & 0x000F_FFFF_FFFF_F000)) as *mut u64;
        let pdpt_0 = *pdpt;
        if pdpt_0 & 1 == 0 {
            crate::serial::write_str("page: PDPT[0] absent, cannot map low 1G\n");
            return;
        }
        if pdpt_0 & (1 << 7) != 0 {
            crate::serial::write_str("page: PDPT[0] is already a 1G page, nothing to do\n");
            return;
        }
        let pd = (phys_offset + (pdpt_0 & 0x000F_FFFF_FFFF_F000)) as *mut u64;
        // C heap's physical range [0x10000000, 0x16000000): PD indices
        // 128..176 (the heap moved above the enlarged kernel image that now
        // embeds the /home test files).  Every other mapping (kernel image,
        // bootloader structures) stays exactly as the bootloader left it.
        for i in 128..176 {
            // PDE: present, writable, 2 MiB page (PS = bit 7).
            *pd.add(i) = ((i as u64) * 0x20_0000) | 0x83;
        }
        // No stack alias mapping needed: the kernel stack is pinned below
        // 4 GiB (0x3000_0000) so mingw-truncated stack pointers are valid.
        // No explicit TLB flush needed: the pages we just wrote either had
        // no TLB entries (heap/stack alias ranges) or map the exact same
        // physical frames as before, so any stale entries remain correct.
        // The page walk sees the new PDEs directly from memory.
        crate::serial::write_str("page: low 1G identity-mapped\n");
    }
}

/// Translate a virtual address to a physical one by walking the 4-level
/// page tables rooted at `cr3`.  Returns 0 if any level is not present.
#[allow(dead_code)]
unsafe fn walk(phys_offset: u64, cr3: u64, virt: u64) -> u64 {
    let idx = |shift: u32| ((virt >> shift) & 0x1FF) as usize;
    let mask = 0x000F_FFFF_FFFF_F000u64;
    let e = |table: *const u64, i: usize| unsafe { *table.add(i) };
    let pml4 = (phys_offset + (cr3 & mask)) as *const u64;
    let e4 = e(pml4, idx(39));
    if e4 & 1 == 0 {
        return 0;
    }
    let pdpt = (phys_offset + (e4 & mask)) as *const u64;
    let e3 = e(pdpt, idx(30));
    if e3 & 1 == 0 {
        return 0;
    }
    if e3 & (1 << 7) != 0 {
        return (e3 & 0x000F_FFFF_C000_0000) | (virt & 0x3FFF_FFFF);
    }
    let pd = (phys_offset + (e3 & mask)) as *const u64;
    let e2 = e(pd, idx(21));
    if e2 & 1 == 0 {
        return 0;
    }
    if e2 & (1 << 7) != 0 {
        return (e2 & 0x000F_FFFF_FFE0_0000) | (virt & 0x1F_FFFF);
    }
    let pt = (phys_offset + (e2 & mask)) as *const u64;
    let e1 = e(pt, idx(12));
    if e1 & 1 == 0 {
        return 0;
    }
    (e1 & mask) | (virt & 0xFFF)
}

/// Debug variant of `walk` that returns each table entry along the way.
#[allow(dead_code)]
unsafe fn walk_debug(phys_offset: u64, cr3: u64, virt: u64) -> [u64; 4] {
    let idx = |shift: u32| ((virt >> shift) & 0x1FF) as usize;
    let mask = 0x000F_FFFF_FFFF_F000u64;
    let e = |table: *const u64, i: usize| unsafe { *table.add(i) };
    let pml4 = (phys_offset + (cr3 & mask)) as *const u64;
    let e4 = e(pml4, idx(39));
    let mut out = [e4, 0, 0, 0];
    if e4 & 1 == 0 {
        return out;
    }
    let pdpt = (phys_offset + (e4 & mask)) as *const u64;
    let e3 = e(pdpt, idx(30));
    out[1] = e3;
    if e3 & 1 == 0 {
        return out;
    }
    let pd = (phys_offset + (e3 & mask)) as *const u64;
    let e2 = e(pd, idx(21));
    out[2] = e2;
    if e2 & 1 == 0 {
        return out;
    }
    let pt = (phys_offset + (e2 & mask)) as *const u64;
    out[3] = e(pt, idx(12));
    out
}

