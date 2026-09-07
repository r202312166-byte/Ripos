//! Build script: merges the LP64 SysV-ABI CPython ELF archive (built with
//! `zig cc -target x86_64-linux-musl`, true LP64 `long`) and the C shim into
//! one relocatable ELF object that gets linked into the kernel.
//!
//! The CPython objects are already ELF64 with correct relocations, so no
//! COFF->ELF conversion or addend patching is needed (unlike the old mingw
//! pipeline).  The shim is compiled with the same zig musl toolchain so all
//! struct layouts (timespec, stat, wchar_t=4, ...) agree with the objects.

use std::path::{Path, PathBuf};
use std::process::Command;

const AR: &str = "C:\\msys64\\mingw64\\bin\\ar.exe";
const ZIG: &str = "D:\\zig\\zig-x86_64-windows-0.14.1\\zig.exe";
const MSYS_BIN: &str = "C:\\msys64\\mingw64\\bin;C:\\msys64\\usr\\bin";

fn run(prog: &str, args: &[&str], dir: &Path) {
    let status = Command::new(prog)
        .args(args)
        .current_dir(dir)
        .env("PATH", format!("{MSYS_BIN};{}", std::env::var("PATH").unwrap_or_default()))
        .status()
        .expect("failed to spawn process");
    assert!(status.success(), "command failed: {prog} {args:?}");
}
/// Compile one C file with the same zig/musl freestanding flags used for the
/// shim and the HACL modules.  `incs` are extra -I directories; `defs` are
/// extra -D flags.  The object lands in `work/<obj>` and is picked up by the
/// merge step below.
fn zig_cc(work: &Path, vendor: &Path, manifest: &Path, src: &Path, obj: &str, incs: &[&Path], defs: &[&str]) {
    let mut args: Vec<String> = vec![
        "cc".into(),
        "-target".into(),
        "x86_64-linux-musl".into(),
        "-c".into(),
        src.to_str().unwrap().into(),
        "-o".into(),
        work.join(obj).to_str().unwrap().into(),
        "-fPIC".into(),
        "-fno-builtin".into(),
        "-fno-stack-protector".into(),
        "-mno-red-zone".into(),
        "-fvisibility=hidden".into(),
        "-fno-asynchronous-unwind-tables".into(),
        "-std=c11".into(),
        "-O2".into(),
        "-D_DEFAULT_SOURCE".into(),
        "-D_BSD_SOURCE".into(),
        "-I".into(),
        vendor.join("Include").to_str().unwrap().into(),
        "-I".into(),
        vendor.join("Include/internal").to_str().unwrap().into(),
        "-I".into(),
        vendor.to_str().unwrap().into(),
    ];
    for i in incs {
        args.push("-I".into());
        args.push(i.to_str().unwrap().into());
    }
    for d in defs {
        args.push(d.to_string());
    }
    let refs: Vec<&str> = args.iter().map(|s| s.as_str()).collect();
    run(ZIG, &refs, manifest);
}


fn main() {
    let manifest = PathBuf::from(env!("CARGO_MANIFEST_DIR"));
    // Raw path (with "..") is fine for ar/zig; bsdtar gets a canonical one
    // below because it misbehaves with ".." in -C on Windows.
    let vendor = manifest.join("../../vendor/Python-3.12.10-lp64");
    let archive = vendor.join("libpython3.12.a");
    assert!(archive.exists(), "missing archive: {}", archive.display());

    let out = PathBuf::from(std::env::var_os("OUT_DIR").unwrap());
    let work = out.join("pyobj");
    // Remove stale artifacts from previous builds so old COFF-converted
    // objects can't leak into the merge.
    if work.exists() {
        std::fs::remove_dir_all(&work).unwrap();
    }
    std::fs::create_dir_all(&work).unwrap();

    // 1. Extract the ELF archive members (mingw `ar` is format-agnostic and
    //    simply packs the files; lld reads them as ELF members).
    run(AR, &["x", archive.to_str().unwrap()], &work);
    // The `_sha2` module links against a separate HACL* SHA-2 archive (it is
    // not part of libpython), so extract its member into the merge too.
    let hacl = vendor.join("Modules/_hacl/libHacl_Hash_SHA2.a");
    if hacl.exists() {
        run(AR, &["x", hacl.to_str().unwrap()], &work);
        println!("cargo:rerun-if-changed={}", hacl.display());
    }

    // 2. Compile the C shim with the same LP64 musl toolchain.  Output is
    //    ELF64 directly (no objcopy).
    let shim_c = manifest.join("shim/pyshim.c");
    let shim_o = work.join("pyshim.o");
    let include = vendor.join("Include");
    run(
        ZIG,
        &[
            "cc",
            "-target",
            "x86_64-linux-musl",
            "-c",
            shim_c.to_str().unwrap(),
            "-o",
            shim_o.to_str().unwrap(),
            "-fno-builtin",
            "-fno-stack-protector",
            "-fno-pic",
            "-fno-pie",
            "-mno-red-zone",
            "-fvisibility=hidden",
            "-fno-asynchronous-unwind-tables",
            "-ftls-model=global-dynamic",
            "-std=c11",
            "-O2",
            "-I",
            include.to_str().unwrap(),
            "-I",
            vendor.to_str().unwrap(),
            "-DPy_BUILD_CORE",
        ],
        &manifest,
    );

    // 2b. Recompile the hashlib module objects (HACL*/blake2 + wrappers) with
    //     -fPIC: the archive versions use absolute 32-bit relocations
    //     (R_X86_64_32S) against local symbols, which can fail at the kernel's
    //     static link depending on symbol layout.  -fPIC turns them into
    //     position-independent relocations that always fit.
    let hacl_inc = vendor.join("Modules/_hacl/include");
    let hacl_srcs = [
        ("md5module.o", "Modules/md5module.c"),
        ("Hacl_Hash_MD5.o", "Modules/_hacl/Hacl_Hash_MD5.c"),
        ("sha1module.o", "Modules/sha1module.c"),
        ("Hacl_Hash_SHA1.o", "Modules/_hacl/Hacl_Hash_SHA1.c"),
        ("sha2module.o", "Modules/sha2module.c"),
        ("Hacl_Hash_SHA2.o", "Modules/_hacl/Hacl_Hash_SHA2.c"),
        ("sha3module.o", "Modules/sha3module.c"),
        ("Hacl_Hash_SHA3.o", "Modules/_hacl/Hacl_Hash_SHA3.c"),
        ("blake2module.o", "Modules/_blake2/blake2module.c"),
        ("blake2b_impl.o", "Modules/_blake2/blake2b_impl.c"),
        ("blake2s_impl.o", "Modules/_blake2/blake2s_impl.c"),
    ];
    for (obj, src) in hacl_srcs {
        let src_path = vendor.join(src);
        let out_obj = work.join(obj);
        run(
            ZIG,
            &[
                "cc",
                "-target",
                "x86_64-linux-musl",
                "-c",
                src_path.to_str().unwrap(),
                "-o",
                out_obj.to_str().unwrap(),
                "-fPIC",
                "-fno-builtin",
                "-fno-stack-protector",
                "-mno-red-zone",
                "-fvisibility=hidden",
                "-fno-asynchronous-unwind-tables",
                "-std=c11",
                "-O2",
                "-I",
                include.to_str().unwrap(),
                "-I",
                vendor.to_str().unwrap(),
                "-I",
                vendor.join("Include/internal").to_str().unwrap(),
                "-I",
                hacl_inc.to_str().unwrap(),
                "-D_BSD_SOURCE",
                "-D_DEFAULT_SOURCE",
                "-DPy_BUILD_CORE",
            ],
            &manifest,
        );
    }


    // 2c. M9.1 format modules: zlib, _bz2, _lzma and _elementtree (expat).
    //     The libraries are vendored next to the kernel build; the module
    //     sources come from the CPython tree.  They are registered at boot
    //     via PyImport_AppendInittab in pyshim.c (the prebuilt libpython
    //     archive does not know about them).
    let work_p = work.clone();
    let vendor_p = vendor.to_path_buf();
    let manifest_p = manifest.to_path_buf();

    // --- zlib (zlib-1.3.1, public domain) --------------------------------
    let zlib_dir = manifest.join("../../vendor/zlib-1.3.1");
    assert!(zlib_dir.join("zlib.h").exists(), "missing zlib: {}", zlib_dir.display());
    let zlib_srcs = [
        "adler32.c", "compress.c", "crc32.c", "deflate.c", "infback.c",
        "inffast.c", "inflate.c", "inftrees.c", "trees.c", "uncompr.c", "zutil.c",
    ];
    for s in zlib_srcs {
        zig_cc(&work_p, &vendor_p, &manifest_p, &zlib_dir.join(s), &format!("zlib_{s}.o"), &[&zlib_dir], &[]);
    }
    zig_cc(
        &work_p, &vendor_p, &manifest_p,
        &vendor.join("Modules/zlibmodule.c"),
        "zlibmodule.o",
        &[&zlib_dir],
        &["-DPy_BUILD_CORE"],
    );

    // --- bzip2 (bzip2-1.0.8, BSD-style license) --------------------------
    let bz_dir = manifest.join("../vendor/bzip2-1.0.8");
    assert!(bz_dir.join("bzlib.h").exists(), "missing bzip2: {}", bz_dir.display());
    let bz_srcs = [
        "bzlib.c", "blocksort.c", "compress.c", "crctable.c",
        "decompress.c", "huffman.c", "randtable.c",
    ];
    for s in bz_srcs {
        zig_cc(&work_p, &vendor_p, &manifest_p, &bz_dir.join(s), &format!("bz_{s}.o"), &[&bz_dir], &[]);
    }
    zig_cc(
        &work_p, &vendor_p, &manifest_p,
        &vendor.join("Modules/_bz2module.c"),
        "_bz2module.o",
        &[&bz_dir],
        &["-DPy_BUILD_CORE"],
    );

    // --- liblzma (xz-5.4.6, public domain) -------------------------------
    let xz_dir = manifest.join("../vendor/xz-5.4.6");
    assert!(xz_dir.join("src/liblzma/api/lzma.h").exists(), "missing xz: {}", xz_dir.display());
    let lzma_incs: Vec<PathBuf> = [
        "src/liblzma/api",
        "src/liblzma",
        "src/liblzma/common",
        "src/liblzma/check",
        "src/liblzma/delta",
        "src/liblzma/lz",
        "src/liblzma/lzma",
        "src/liblzma/rangecoder",
        "src/liblzma/simple",
        "src/common",
    ]
    .iter()
    .map(|d| xz_dir.join(d))
    .collect();
    let lzma_inc_refs: Vec<&Path> = lzma_incs.iter().map(|p| p.as_path()).collect();
    // Walk liblzma for all .c files, skipping host generators, the MT
    // encoders (threads) and unused API helpers.
    let mut lzma_srcs: Vec<PathBuf> = Vec::new();
    for dir in ["check", "common", "delta", "lz", "lzma", "rangecoder", "simple"] {
        let d = xz_dir.join("src/liblzma").join(dir);
        if let Ok(rd) = std::fs::read_dir(&d) {
            for e in rd.flatten() {
                let p = e.path();
                if p.extension().map_or(true, |x| x != "c") {
                    continue;
                }
                let f = p.file_name().unwrap().to_string_lossy().into_owned();
                // NB: only the *multithreaded* encoder files are excluded by
                // name -- a generic "contains m t" filter would also catch
                // simple/armthumb.c ("armt humb").
                if f.contains("tablegen")
                    || f.contains("_small")
                    || f == "outqueue.c"
                    || f == "stream_encoder_mt.c"
                    || f == "stream_decoder_mt.c"
                    || f == "hardware_physmem.c"
                    || f == "hardware_cputhreads.c"
                    || f == "string_conversion.c"
                    || f == "file_info.c"
                    || f == "lzip_decoder.c"
                    || f.starts_with("microlzma")
                {
                    continue;
                }
                lzma_srcs.push(p);
            }
        }
    }
    lzma_srcs.sort();
    // Enable the checks, the encoders/decoders and the match finders: without
    // these the XZ encoder rejects its default CRC64 check and every filter
    // with LZMA_OPTIONS_ERROR ("Invalid or unsupported options").
    let lzma_defs = [
        "-DSIZEOF_SIZE_T=8",
        "-DHAVE_STDBOOL_H",
        "-DHAVE_STDINT_H",
        "-DHAVE_CHECK_CRC32",
        "-DHAVE_CHECK_CRC64",
        "-DHAVE_CHECK_SHA256",
        "-DHAVE_INTERNAL_SHA256",
        "-DHAVE_ENCODER_LZMA1",
        "-DHAVE_ENCODER_LZMA2",
        "-DHAVE_ENCODER_DELTA",
        "-DHAVE_ENCODER_X86",
        "-DHAVE_ENCODER_POWERPC",
        "-DHAVE_ENCODER_IA64",
        "-DHAVE_ENCODER_ARM",
        "-DHAVE_ENCODER_ARMTHUMB",
        "-DHAVE_ENCODER_ARM64",
        "-DHAVE_ENCODER_SPARC",
        "-DHAVE_DECODER_LZMA1",
        "-DHAVE_DECODER_LZMA2",
        "-DHAVE_DECODER_DELTA",
        "-DHAVE_DECODER_X86",
        "-DHAVE_DECODER_POWERPC",
        "-DHAVE_DECODER_IA64",
        "-DHAVE_DECODER_ARM",
        "-DHAVE_DECODER_ARMTHUMB",
        "-DHAVE_DECODER_ARM64",
        "-DHAVE_DECODER_SPARC",
        "-DHAVE_MF_BT2",
        "-DHAVE_MF_BT3",
        "-DHAVE_MF_BT4",
        "-DHAVE_MF_HC3",
        "-DHAVE_MF_HC4",
    ];
    for p in &lzma_srcs {
        let rel = p.strip_prefix(&xz_dir).unwrap().to_string_lossy().replace('/', "_").replace('\\', "_");
        zig_cc(&work_p, &vendor_p, &manifest_p, p, &format!("lzma_{rel}.o"), &lzma_inc_refs, &lzma_defs);
    }
    zig_cc(
        &work_p, &vendor_p, &manifest_p,
        &vendor.join("Modules/_lzmamodule.c"),
        "_lzmamodule.o",
        &lzma_inc_refs,
        &["-DPy_BUILD_CORE", "-DSIZEOF_SIZE_T=8"],
    );

    // --- _elementtree (CPython's vendored expat) --------------------------
    let expat_dir = vendor.join("Modules/expat");
    let expat_srcs = ["xmlparse.c", "xmlrole.c", "xmltok.c", "xmltok_impl.c", "xmltok_ns.c"];
    for s in expat_srcs {
        zig_cc(
            &work_p, &vendor_p, &manifest_p,
            &expat_dir.join(s),
            &format!("expat_{s}.o"),
            &[&expat_dir],
            &["-DHAVE_EXPAT_CONFIG_H", "-DXML_STATIC"],
        );
    }
    zig_cc(
        &work_p, &vendor_p, &manifest_p,
        &vendor.join("Modules/_elementtree.c"),
        "_elementtree.o",
        &[&expat_dir],
        &["-DPy_BUILD_CORE", "-DHAVE_EXPAT_CONFIG_H", "-DXML_STATIC",
          "-DUSE_PYEXPAT_CAPI"],
    );
    // pyexpat: _elementtree's C parser backend (it imports pyexpat at init).
    zig_cc(
        &work_p, &vendor_p, &manifest_p,
        &vendor.join("Modules/pyexpat.c"),
        "pyexpat.o",
        &[&expat_dir],
        &["-DPy_BUILD_CORE", "-DHAVE_EXPAT_H", "-DXML_STATIC", "-DUSE_PYEXPAT_CAPI"],
    );

    // --- M9.5: minimp3 MP3 decoder (_mp3dec) ----------------------------
    // The single-header decoder lives in kernel/shim (vendored next to the
    // shim so the same include dir serves both); the Python wrapper module
    // is registered via PyImport_AppendInittab in pyshim.c.
    let shim_dir = manifest.join("shim");
    zig_cc(
        &work_p, &vendor_p, &manifest_p,
        &shim_dir.join("mp3decmod.c"),
        "mp3decmod.o",
        &[&shim_dir],
        &["-DPy_BUILD_CORE"],
    );

    // --- M9.7: stb_image image decode (_img) ---------------------------
    // C-speed PNG/JPEG/GIF/BMP decode (stb_image.h, public domain); the
    // pure-Python jpeg.py/png.py fall back when this module is absent.
    zig_cc(
        &work_p, &vendor_p, &manifest_p,
        &shim_dir.join("imgmod.c"),
        "imgmod.o",
        &[&shim_dir],
        &["-DPy_BUILD_CORE"],
    );

    // --- M10.1: h264bsd H.264 baseline decoder (_h264) -------------------
    // Vendored in shim/h264bsd (Apache-2.0, Android/Nokia heritage).  The
    // wrapper module is registered via PyImport_AppendInittab in pyshim.c.
    let h264_dir = manifest.join("shim/h264bsd");
    let h264_incs: Vec<&Path> = vec![&shim_dir, &h264_dir];
    let h264_srcs = [
        "h264bsd_byte_stream.c", "h264bsd_cavlc.c", "h264bsd_conceal.c",
        "h264bsd_deblocking.c", "h264bsd_decoder.c", "h264bsd_dpb.c",
        "h264bsd_image.c", "h264bsd_inter_prediction.c",
        "h264bsd_intra_prediction.c", "h264bsd_macroblock_layer.c",
        "h264bsd_nal_unit.c", "h264bsd_neighbour.c", "h264bsd_pic_order_cnt.c",
        "h264bsd_pic_param_set.c", "h264bsd_reconstruct.c", "h264bsd_sei.c",
        "h264bsd_seq_param_set.c", "h264bsd_slice_data.c",
        "h264bsd_slice_group_map.c", "h264bsd_slice_header.c",
        "h264bsd_storage.c", "h264bsd_stream.c", "h264bsd_transform.c",
        "h264bsd_util.c", "h264bsd_vlc.c", "h264bsd_vui.c",
    ];
    for s in h264_srcs {
        zig_cc(
            &work_p, &vendor_p, &manifest_p,
            &h264_dir.join(s),
            &format!("h264bsd_{s}.o"),
            &h264_incs,
            &[],
        );
    }
    zig_cc(
        &work_p, &vendor_p, &manifest_p,
        &shim_dir.join("h264decmod.c"),
        "h264decmod.o",
        &h264_incs,
        &["-DPy_BUILD_CORE"],
    );

    // 3. Merge all ELF objects into one relocatable with rust-lld.
    let sysroot = String::from_utf8(
        Command::new("rustc")
            .arg("--print")
            .arg("sysroot")
            .output()
            .expect("rustc sysroot")
            .stdout,
    )
    .unwrap();
    let sysroot = sysroot.trim();
    let host = std::env::var("HOST").unwrap_or_else(|_| "x86_64-pc-windows-gnu".into());
    let lld = Path::new(sysroot)
        .join("lib")
        .join("rustlib")
        .join(&host)
        .join("bin")
        .join("rust-lld.exe");
    assert!(lld.exists(), "rust-lld not found: {}", lld.display());

    let merged = out.join("libpython_merged.o");
    let mut args: Vec<&str> = vec![
        "-flavor",
        "gnu",
        "-r",
        "--allow-multiple-definition",
        "-o",
        merged.to_str().unwrap(),
    ];
    let mut objs: Vec<String> = Vec::new();
    for entry in std::fs::read_dir(&work).unwrap() {
        let entry = entry.unwrap();
        let path = entry.path();
        if path.extension().map_or(false, |e| e == "o") {
            objs.push(path.to_str().unwrap().to_string());
        }
    }
    objs.sort();
    // Windows command lines are capped at ~32K: with ~280 objects the merge
    // command would exceed it, so pass the object list through an lld
    // response file instead (each argument on its own line, quoted).
    let rsp = out.join("merge.rsp");
    let mut rsp_txt = String::new();
    for o in &objs {
        rsp_txt.push('"');
        rsp_txt.push_str(o);
        rsp_txt.push_str("\"\n");
    }
    std::fs::write(&rsp, rsp_txt).unwrap();
    let rsp_arg = format!("@{}", rsp.display());
    let mut margs: Vec<&str> = vec![
        "-flavor",
        "gnu",
        "-r",
        "--allow-multiple-definition",
        "-o",
        merged.to_str().unwrap(),
        &rsp_arg,
    ];
    run(lld.to_str().unwrap(), &margs, &out);

    // 4. Build the initramfs (CPython Lib/ subset) into OUT_DIR so the
    //    kernel can `include_bytes!` it.  Pure-Python stdlib lives here;
    //    tests/GUI/installers are excluded to keep the image small.  The
    //    archive is written directly in ustar format (no external tools).
    let lib = vendor.join("Lib");
    let extra = manifest.join("initramfs_extra");
    let tar = out.join("initramfs.tar");
    let mut data: Vec<u8> = Vec::new();
    let mut skip = |path: &std::path::Path| -> bool {
        let n = path.file_name().and_then(|s| s.to_str()).unwrap_or("");
        n == "test"
            || n == "tests"
            || n == "__pycache__"
            || n == "idlelib"
            || n == "turtledemo"
            || n == "tkinter"
            || n == "ensurepip"
            || n == "socket"
            || n == "socket.py"   // our pure-Python socket shim (Phase 2)
            || n == "lib2to3"
            || n.ends_with(".pyc")
            || n.ends_with(".pyo")
    };
    fn walk(dir: &std::path::Path, prefix: &str, skip: &dyn Fn(&std::path::Path) -> bool, out: &mut Vec<u8>) {
        let mut entries: Vec<_> = std::fs::read_dir(dir)
            .unwrap()
            .map(|e| e.unwrap().path())
            .collect();
        entries.sort();
        for p in &entries {
            if skip(p) {
                continue;
            }
            let name = p.file_name().unwrap().to_str().unwrap().to_string();
            let rel = format!("{prefix}{name}");
            if p.is_dir() {
                let rel = format!("{rel}/");
                write_ustar(out, &rel, b"5", 0, b"0000755\0");
                walk(p, &rel, skip, out);
            } else {
                let bytes = std::fs::read(p).unwrap();
                write_ustar(out, &rel, b"0", bytes.len() as u64, b"0000644\0");
                out.extend_from_slice(&bytes);
                let pad = (512 - (bytes.len() % 512)) % 512;
                out.extend(std::iter::repeat(0u8).take(pad));
            }
        }
    }
    // Paths are stored with a leading "./" like classic tar.
    fn write_ustar(out: &mut Vec<u8>, name: &str, typeflag: &[u8], size: u64, mode: &[u8]) {
        let mut h = [0u8; 512];
        let n = name.as_bytes();
        h[..n.len()].copy_from_slice(n);
        h[100..108].copy_from_slice(mode); // mode
        // uid/gid
        h[108..116].copy_from_slice(b"0000000\0");
        h[116..124].copy_from_slice(b"0000000\0");
        // size (octal, 11 digits + NUL)
        let sz = format!("{size:011o}\0");
        h[124..136].copy_from_slice(sz.as_bytes());
        // mtime
        h[136..148].copy_from_slice(b"00000000000\0");
        // checksum: spaces for now
        h[148..156].fill(b' ');
        h[156] = typeflag[0];
        h[257..263].copy_from_slice(b"ustar\0");
        h[263..265].copy_from_slice(b"00");
        h[265..270].copy_from_slice(b"root\0"); // uname
        h[297..302].copy_from_slice(b"root\0"); // gname
        // checksum: sum of all bytes
        let sum: u64 = h.iter().map(|&b| b as u64).sum();
        let cks = format!("{sum:06o}\0 ");
        h[148..156].copy_from_slice(cks.as_bytes());
        out.extend_from_slice(&h);
    }
    walk(&lib, "./", &skip, &mut data);
    // The extra tree is the whole point (no skips), but never embed
    // compiler droppings (py_compile runs on the host during development)
    // or oversized media fixtures: a 150 MB test video only bloats the
    // boot image and slows SeaBIOS disk reads under TCG.  Host gates read
    // those files straight off the source tree.
    // RIPOS_SLIM_INITRAMFS=1 drops the big media fixtures so the kernel
    // stays under the 32 MiB El Torito hard-disk-emulation limit (used to
    // produce the bootable ISO via tools/make_iso.py).
    let slim = std::env::var_os("RIPOS_SLIM_INITRAMFS").is_some();
    let extra_skip = |path: &std::path::Path| -> bool {
        let n = path.file_name().and_then(|s| s.to_str()).unwrap_or("");
        if n == "__pycache__" {
            return true;
        }
        if slim && path.is_file() {
            // the media fixtures (only used by the demo gates)
            if matches!(n, "test.zip" | "testaudio.MP3" | "testvideo-mjpeg.mp4"
                        | "testvideo-h264.mp4" | "testanim.gif" | "test.zip") {
                return true;
            }
        }
        if path.is_file() {
            match std::fs::metadata(path) {
                Ok(m) if m.len() > 64 * 1024 * 1024 => return true,
                _ => {}
            }
        }
        false
    };
    walk(&extra, "./", &extra_skip, &mut data);
    // End-of-archive marker (two zero blocks).
    data.extend(std::iter::repeat(0u8).take(1024));
    std::fs::write(&tar, &data).unwrap();
    println!("initramfs: {} bytes", data.len());
    println!("cargo:rerun-if-changed={}", lib.display());
    println!("cargo:rerun-if-changed={}", extra.display());

    // 5. Tell rustc to link the merged object.
    println!("cargo:rustc-link-arg={}", merged.display());

    // Rebuild when the archive or shim changes.
    println!("cargo:rerun-if-changed={}", archive.display());
    println!("cargo:rerun-if-changed={}", shim_c.display());
    println!("cargo:rerun-if-changed={}", shim_dir.join("mp3decmod.c").display());
    println!("cargo:rerun-if-changed={}", shim_dir.join("imgmod.c").display());
    println!("cargo:rerun-if-changed={}", shim_dir.join("stb_image.h").display());
    println!("cargo:rerun-if-changed={}", shim_dir.join("minimp3.h").display());
    println!("cargo:rerun-if-changed={}", shim_dir.join("h264decmod.c").display());
    println!("cargo:rerun-if-changed={}", shim_dir.join("h264bsd").display());
}
