//! Host de-risk harness: embed CPython via hand-written FFI.
//!
//! Validates the "Rust core embeds the interpreter" contract before the
//! freestanding kernel port. Linked against `python314.lib` (import library)
//! shipped with the installed Python 3.14 -- no PyO3, no bindgen: the C API
//! surface is declared by hand here, exactly as it will be in the kernel.

#![allow(non_snake_case)]
#![allow(non_upper_case_globals)]

use std::ffi::{c_char, c_int, c_longlong, CStr, CString};

/// Opaque CPython object pointer (every Python value is a `PyObject*`).
#[repr(C)]
pub struct PyObject {
    _private: [u8; 0],
}

/// Hand-written declarations of the subset of the CPython C API we use.
/// Signatures verified against the installed 3.14 headers.
mod ffi {
    use super::PyObject;
    use std::ffi::{c_char, c_int, c_longlong};

    // start flags for the Py_CompileString family (pythonrun.h)
    pub const Py_file_input: c_int = 257;
    pub const Py_eval_input: c_int = 258;

    extern "C" {
        pub fn Py_Initialize();
        pub fn Py_IsInitialized() -> c_int;
        pub fn Py_FinalizeEx() -> c_int;
        pub fn Py_GetVersion() -> *const c_char;

        pub fn Py_CompileString(
            code: *const c_char,
            filename: *const c_char,
            start: c_int,
        ) -> *mut PyObject;
        pub fn PyEval_EvalCode(
            code: *mut PyObject,
            globals: *mut PyObject,
            locals: *mut PyObject,
        ) -> *mut PyObject;

        pub fn PyEval_GetBuiltins() -> *mut PyObject;
        pub fn PyDict_New() -> *mut PyObject;
        pub fn PyDict_SetItemString(
            dict: *mut PyObject,
            key: *const c_char,
            value: *mut PyObject,
        ) -> c_int;

        pub fn PyObject_Str(o: *mut PyObject) -> *mut PyObject;
        pub fn PyUnicode_AsUTF8AndSize(u: *mut PyObject, size: *mut c_longlong) -> *const c_char;

        pub fn PyErr_Print();
        #[allow(dead_code)]
        pub fn PyErr_Clear();

        pub fn Py_DecRef(o: *mut PyObject);
        #[allow(dead_code)]
        pub fn Py_IncRef(o: *mut PyObject);
    }
}

/// Owns a CPython interpreter instance. Dropping it finalizes CPython.
pub struct Python;

impl Python {
    pub fn init() -> Self {
        unsafe { ffi::Py_Initialize() };
        assert_eq!(
            unsafe { ffi::Py_IsInitialized() },
            1,
            "Py_Initialize failed"
        );
        Python
    }

    /// Compile `code` and evaluate it in a fresh module namespace.
    ///
    /// We deliberately avoid `PyRun_*` (deprecated in 3.14) and never pass a
    /// NULL namespace (a fragile path in newer CPython). Every evaluation gets
    /// its own global dict seeded with `__builtins__` so `print`, `range`,
    /// list comprehensions, etc. all resolve -- the same model the kernel will
    /// expose to OS services. Returns the result object (owned, 1 ref).
    fn compile_and_eval(&self, code: &str, filename: &str, start: c_int) -> Result<*mut PyObject, ()> {
        let c_code = cstr(code).map_err(|_| ())?;
        let c_file = cstr(filename).map_err(|_| ())?;
        unsafe {
            let globals = ffi::PyDict_New();
            if globals.is_null() {
                ffi::PyErr_Print();
                return Err(());
            }
            let rc = ffi::PyDict_SetItemString(
                globals,
                b"__builtins__\0".as_ptr() as *const c_char,
                ffi::PyEval_GetBuiltins(),
            );
            if rc != 0 {
                ffi::PyErr_Print();
                ffi::Py_DecRef(globals);
                return Err(());
            }
            let co = ffi::Py_CompileString(c_code.as_ptr(), c_file.as_ptr(), start);
            if co.is_null() {
                ffi::PyErr_Print();
                ffi::Py_DecRef(globals);
                return Err(());
            }
            let res = ffi::PyEval_EvalCode(co, globals, globals);
            ffi::Py_DecRef(co);
            ffi::Py_DecRef(globals);
            if res.is_null() {
                ffi::PyErr_Print();
                return Err(());
            }
            Ok(res)
        }
    }

    /// Run Python *statements* (`exec` semantics). Any exception is printed
    /// to the traceback stream and returned as `Err`.
    pub fn exec(&self, code: &str) -> Result<(), String> {
        let res = self
            .compile_and_eval(code, "<host_embed>", ffi::Py_file_input)
            .map_err(|()| "exec failed".to_string())?;
        unsafe { ffi::Py_DecRef(res) };
        Ok(())
    }

    /// Evaluate a Python *expression* and return its `str()` representation.
    pub fn eval_str(&self, code: &str) -> Result<String, String> {
        let res = self
            .compile_and_eval(code, "<host_embed_eval>", ffi::Py_eval_input)
            .map_err(|()| "eval failed".to_string())?;
        unsafe {
            let s = ffi::PyObject_Str(res);
            ffi::Py_DecRef(res);
            if s.is_null() {
                ffi::PyErr_Print();
                return Err("str() failed".to_string());
            }
            let text = py_unicode_to_string(s)?;
            ffi::Py_DecRef(s);
            Ok(text)
        }
    }
}

impl Drop for Python {
    fn drop(&mut self) {
        unsafe {
            ffi::Py_FinalizeEx();
        }
    }
}

fn cstr(s: &str) -> Result<CString, String> {
    CString::new(s).map_err(|e| format!("NUL byte in string: {e}"))
}

/// Convert a Python `str` object to a Rust `String` (copying the data out).
fn py_unicode_to_string(u: *mut PyObject) -> Result<String, String> {
    unsafe {
        let mut size: c_longlong = 0;
        let ptr = ffi::PyUnicode_AsUTF8AndSize(u, &mut size);
        if ptr.is_null() {
            ffi::PyErr_Print();
            return Err("Unicode conversion failed".into());
        }
        let bytes = core::slice::from_raw_parts(ptr as *const u8, size.max(0) as usize);
        Ok(String::from_utf8_lossy(bytes).into_owned())
    }
}

fn main() {
    println!("[rust] Interpretive OS -- host de-risk harness (hand-written CPython FFI)");

    let py = Python::init();
    let ver = unsafe { CStr::from_ptr(ffi::Py_GetVersion()) }
        .to_str()
        .unwrap_or("?");
    println!("[rust] CPython {ver} initialized");

    // 1. The interpreter core runs OS-level code (pure Python, binary-free).
    py.exec(
        r#"
print("Python core alive: hello from inside the interpreter")
print("this line was produced by Python bytecode, not by Rust")
"#,
    )
    .expect("exec failed");

    // 2. Data flows Python -> Rust so the core can drive OS services.
    let answer = py.eval_str("40 + 2").expect("eval failed");
    println!("[rust] Python evaluated `40 + 2` -> {answer}");

    let squares = py.eval_str("[x * x for x in range(6)]").expect("eval failed");
    println!("[rust] Python built {squares}");

    // 3. Exceptions propagate back to Rust cleanly.
    match py.eval_str("1 / 0") {
        Ok(v) => println!("[rust] UNEXPECTED ok: {v}"),
        Err(_) => println!("[rust] caught Python ZeroDivisionError (traceback above) as expected"),
    }

    println!("[rust] finalizing interpreter...");
    drop(py);
    println!("[rust] done");
}