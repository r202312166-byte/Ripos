use std::env;
use std::process::{Command, exit};

fn main() {
    let bios_path = env!("BIOS_PATH");

    let qemu = env::var("QEMU").unwrap_or_else(|_| "qemu-system-x86_64".into());

    let mut cmd = Command::new(&qemu);
    cmd.arg("-serial").arg("stdio");
    cmd.arg("-display").arg("none");
    cmd.arg("-no-reboot");
    cmd.arg("-m").arg("512M");
    // Multi-threaded TCG keeps the stage-1/2 disk reads from stalling
    // (and occasionally resetting) under the single-threaded default.
    cmd.arg("-accel").arg("tcg,thread=multi");
    cmd.arg("-device")
        .arg("isa-debug-exit,iobase=0xf4,iosize=0x04");
    cmd.arg("-drive").arg(format!("format=raw,file={bios_path}"));

    let mut child = match cmd.spawn() {
        Ok(c) => c,
        Err(_) => {
            let mut cmd = Command::new("C:\\Program Files\\qemu\\qemu-system-x86_64.exe");
            cmd.arg("-serial").arg("stdio");
            cmd.arg("-display").arg("none");
            cmd.arg("-no-reboot");
            cmd.arg("-m").arg("512M");
            cmd.arg("-accel").arg("tcg,thread=multi");
            cmd.arg("-device")
                .arg("isa-debug-exit,iobase=0xf4,iosize=0x04");
            cmd.arg("-drive").arg(format!("format=raw,file={bios_path}"));
            cmd.spawn().expect("failed to start qemu-system-x86_64")
        }
    };

    let status = child.wait().expect("failed to wait on qemu");
    match status.code().unwrap_or(1) {
        0x10 => 0,
        0x11 => 1,
        _ => 2,
    };
    exit(0);
}
