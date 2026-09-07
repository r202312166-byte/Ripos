use core::fmt::{self, Write};
use core::sync::atomic::{AtomicUsize, Ordering};

const VGA_ADDR: usize = 0xB8000;
const COLS: usize = 80;
const ROWS: usize = 25;

static ROW: AtomicUsize = AtomicUsize::new(0);
static COL: AtomicUsize = AtomicUsize::new(0);

const COLOR: u8 = 0x0F; // white on black

fn buffer() -> &'static mut [[u16; COLS]; ROWS] {
    unsafe { &mut *(VGA_ADDR as *mut [[u16; COLS]; ROWS]) }
}

fn put_char_raw(c: u8, row: usize, col: usize) {
    buffer()[row][col] = ((COLOR as u16) << 8) | c as u16;
}

fn scroll() {
    let buf = buffer();
    for row in 1..ROWS {
        for col in 0..COLS {
            let v = buf[row][col];
            buf[row - 1][col] = v;
        }
    }
    for col in 0..COLS {
        put_char_raw(b' ', ROWS - 1, col);
    }
}

fn advance() {
    let mut row = ROW.load(Ordering::Relaxed);
    let mut col = COL.load(Ordering::Relaxed);
    col += 1;
    if col >= COLS {
        col = 0;
        row += 1;
    }
    if row >= ROWS {
        scroll();
        row = ROWS - 1;
    }
    ROW.store(row, Ordering::Relaxed);
    COL.store(col, Ordering::Relaxed);
}

// VGA text-mode output is currently disabled: the bootloader does not map
// the legacy 0xB8000 text buffer into the kernel page table (it provides a
// real framebuffer instead). Rendering via the framebuffer will be added
// with the GUI phase; all logging goes over the serial console for now.

pub fn write_str(_s: &str) {}

pub fn clear() {}

pub struct VgaWriter;

impl fmt::Write for VgaWriter {
    fn write_str(&mut self, s: &str) -> fmt::Result {
        write_str(s);
        Ok(())
    }
}

pub fn write_fmt(args: fmt::Arguments) {
    let _ = VgaWriter.write_fmt(args);
}
