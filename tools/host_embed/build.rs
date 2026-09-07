use std::env;
use std::path::Path;

fn main() {
    let libs = env::var("PYTHON_LIBS").unwrap_or_else(|_| {
        r"C:\Users\Administrator\AppData\Local\Python\pythoncore-3.14-64\libs".to_string()
    });
    assert!(Path::new(&libs).is_dir(), "PYTHON_LIBS dir not found: {libs}");
    println!("cargo:rustc-link-search=native={libs}");
    println!("cargo:rustc-link-lib=python314");
    println!("cargo:rerun-if-env-changed=PYTHON_LIBS");
}