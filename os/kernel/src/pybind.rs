//! FFI bindings to the embedded CPython 3.12.

use core::ffi::{c_char, c_int, c_void};

pub const PY_FILE_INPUT: c_int = 257;

#[repr(C)]
pub struct PyObject {
    _priv: [u8; 0],
}

extern "C" {
    pub fn pyboot_start();
    pub fn kern_process_events();
    pub fn kern_import_selftest();
    pub fn Py_IsInitialized() -> c_int;
    pub fn Py_CompileString(
        s: *const c_char,
        file: *const c_char,
        start: c_int,
    ) -> *mut PyObject;
    pub fn PyEval_EvalCode(
        code: *mut PyObject,
        globals: *mut PyObject,
        locals: *mut PyObject,
    ) -> *mut PyObject;
    pub fn PyDict_New() -> *mut PyObject;
    pub fn PyDict_SetItemString(d: *mut PyObject, key: *const c_char, v: *mut PyObject) -> c_int;
    pub fn PyEval_GetBuiltins() -> *mut PyObject;
    pub fn PyErr_Print();
    pub fn PyObject_Str(o: *mut PyObject) -> *mut PyObject;
    pub fn PyUnicode_AsUTF8AndSize(o: *mut PyObject, size: *mut isize) -> *const c_char;
    pub fn Py_DecRef(o: *mut PyObject);
}

/// Compile and evaluate a Python expression/module, printing the result
/// (or the traceback) to the serial console.
pub unsafe fn eval(source: &str, label: &str) {
    unsafe {
        if Py_IsInitialized() == 0 {
            crate::serial::write_str("eval: Python not initialized\n");
            return;
        }
        let src = crate::util::c_str(source);
        let file = crate::util::c_str(label);
        let code = Py_CompileString(src.as_ptr(), file.as_ptr(), PY_FILE_INPUT);
        if code.is_null() {
            crate::serial::write_str("[compile error]\n");
            PyErr_Print();
            return;
        }
        let globals = PyDict_New();
        if globals.is_null() {
            Py_DecRef(code);
            return;
        }
        let builtins = PyEval_GetBuiltins();
        if !builtins.is_null() {
            let key = crate::util::c_str("__builtins__");
            PyDict_SetItemString(globals, key.as_ptr(), builtins);
        }
        let result = PyEval_EvalCode(code, globals, globals);
        if result.is_null() {
            PyErr_Print();
        } else {
            let s = PyObject_Str(result);
            if !s.is_null() {
                let mut size: isize = 0;
                let p = PyUnicode_AsUTF8AndSize(s, &mut size);
                if !p.is_null() && size > 0 {
                    let bytes = core::slice::from_raw_parts(p as *const u8, size as usize);
                    crate::serial::write_bytes(bytes);
                    crate::serial::write_str("\n");
                }
                Py_DecRef(s);
            }
            Py_DecRef(result);
        }
        Py_DecRef(code);
        Py_DecRef(globals);
    }
}
