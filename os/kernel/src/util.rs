//! Small utilities for the kernel.

/// NUL-terminated copy of a string on a fixed stack buffer.
pub struct CStrBuf {
    buf: [u8; 512],
    len: usize,
}

impl CStrBuf {
    pub fn new(s: &str) -> CStrBuf {
        let mut buf = [0u8; 512];
        let n = s.len().min(510);
        buf[..n].copy_from_slice(&s.as_bytes()[..n]);
        buf[n] = 0;
        CStrBuf { buf, len: n + 1 }
    }

    pub fn as_ptr(&self) -> *const core::ffi::c_char {
        self.buf.as_ptr() as *const core::ffi::c_char
    }
}

pub fn c_str(s: &str) -> CStrBuf {
    CStrBuf::new(s)
}
