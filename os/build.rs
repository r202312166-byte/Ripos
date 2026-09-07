//! Build script: builds the kernel for the Ripos SSE target (custom target
//! spec with the standard SysV float ABI -- the stock x86_64-unknown-none is
//! soft-float, which broke every float FFI with C), then creates the bootable
//! BIOS disk image with the bootloader crate.
//!
//! The kernel is built via a cargo subprocess instead of artifact bindeps:
//! bindeps + -Z build-std + -Z json-target-spec hits a cargo bug (panic in
//! unit_dependencies.rs).  RIPOS_BUILDING_KERNEL guards against recursion.

use std::path::PathBuf;
use std::process::Command;

fn main() {
    let out_dir = PathBuf::from(std::env::var_os("OUT_DIR").unwrap());
    let manifest_dir = PathBuf::from(env!("CARGO_MANIFEST_DIR"));

    if std::env::var_os("RIPOS_BUILDING_KERNEL").is_none() {
        // Phase 3.1 (perf): build the kernel in the same profile as the
        // parent crate.  A debug (-O0) kernel under TCG was the biggest
        // source of OS lag; release gives opt-level 2 to the kernel and to
        // the build-std core/alloc.
        let release = std::env::var("PROFILE").map(|p| p == "release").unwrap_or(false);
        let mut args: Vec<String> = vec![
            "-Z".into(),
            "json-target-spec".into(),
            "-Z".into(),
            "build-std=core,alloc".into(),
            "build".into(),
            "-p".into(),
            "interpretive-kernel".into(),
            "--target".into(),
            "ripos-x86_64.json".into(),
            "--target-dir".into(),
            "target/kernel-build".into(),
        ];
        if release {
            args.push("--release".into());
        }
        let status = Command::new("cargo")
            .args(&args)
            .current_dir(&manifest_dir)
            .env("RIPOS_BUILDING_KERNEL", "1")
            .env_remove("CARGO_ENCODED_RUSTFLAGS") // parent cargo overrides RUSTFLAGS via this
            .env(
                "RUSTFLAGS",
                "-Z unstable-options -Z ub-checks=no -C relocation-model=static -C link-arg=-Tkernel/linker.ld",
            )
            .env(
                "PATH",
                format!(
                    "C:\\msys64\\mingw64\\bin;C:\\msys64\\usr\\bin;{}",
                    std::env::var("PATH").unwrap_or_default()
                ),
            )
            .status()
            .expect("failed to spawn kernel build");
        assert!(status.success(), "kernel build failed");
    }

    let profile = if std::env::var("PROFILE").map(|p| p == "release").unwrap_or(false) {
        "release"
    } else {
        "debug"
    };
    let kernel = manifest_dir
        .join("target")
        .join("kernel-build")
        .join("ripos-x86_64")
        .join(profile)
        .join("interpretive-kernel");
    assert!(kernel.exists(), "kernel artifact missing: {}", kernel.display());

    let bios_path = out_dir.join("bios.img");
    bootloader::BiosBoot::new(&kernel)
        .create_disk_image(&bios_path)
        .expect("failed to create BIOS disk image");

    // Canonical copy at target/bios.img: OUT_DIR gets a fresh hash directory
    // on every build, and stale images there made target scripts pick the
    // wrong one.  All harnesses (vbox-setup.ps1, QEMU scripts) use this path.
    let canonical = manifest_dir.join("target").join("bios.img");
    std::fs::copy(&bios_path, &canonical).expect("failed to copy bios.img to target/");
    println!("cargo:rustc-env=BIOS_PATH={}", canonical.display());
    println!("cargo:rerun-if-changed={}", kernel.display());
    // Rebuild the disk image whenever any kernel source changes (the kernel
    // subprocess itself decides whether to recompile).
    println!("cargo:rerun-if-changed={}", manifest_dir.join("kernel").display());
    println!("cargo:rerun-if-changed={}", manifest_dir.join("kernel/build.rs").display());
}
