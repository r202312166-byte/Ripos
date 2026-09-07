/* imgmod.c -- _img builtin module: C-speed image decode via stb_image.
 *
 * Wraps the vendored single-header stb_image.h (public domain) the same
 * way mp3decmod.c wraps minimp3.  The kernel is freestanding, so the
 * module is compiled with the same zig/musl flags as pyshim.c and
 * registered via PyImport_AppendInittab in pyboot_start().
 *
 * API:  _img.decode(data) -> (width, height, 3, rgb_bytes)
 *   data        PNG / baseline+progressive JPEG / GIF / BMP bytes
 *   rgb_bytes   w*h*3 bytes, tightly packed, top-down
 * Callers (png.py / jpeg.py / mplayer.py) fall back to the pure-Python
 * decoders when this module is absent (host test runner).
 */

#define STB_IMAGE_IMPLEMENTATION
#define STBI_ONLY_JPEG
#define STBI_ONLY_PNG
#define STBI_ONLY_GIF
#define STBI_ONLY_BMP
#define STBI_NO_HDR
#define STBI_NO_LINEAR
#define STBI_NO_STDIO
/* The kernel is freestanding: there is no FS-based thread-local storage
   (FS base is 0), so stb_image's __thread flags would fault on access.
   STBI_NO_THREAD_LOCALS turns them into plain statics. */
#define STBI_NO_THREAD_LOCALS
#include "stb_image.h"

#include <Python.h>

/* stb_image needs malloc/free/realloc; the kernel shim provides them via
   libc.rs over the C heap, so the defaults resolve at link time. */

static PyObject *img_decode(PyObject *self, PyObject *args) {
    (void)self;
    Py_buffer view;
    if (!PyArg_ParseTuple(args, "y*", &view)) {
        return NULL;
    }
    if (view.len <= 0 || view.buf == NULL) {
        PyBuffer_Release(&view);
        PyErr_SetString(PyExc_ValueError, "_img.decode: empty input");
        return NULL;
    }
    int w = 0, h = 0, comp = 0;
    unsigned char *pix = stbi_load_from_memory((const unsigned char *)view.buf,
                                               (int)view.len, &w, &h, &comp, 3);
    PyBuffer_Release(&view);
    if (!pix) {
        PyErr_SetString(PyExc_ValueError, "_img.decode: image decode failed");
        return NULL;
    }
    /* Cap: a 4K RGBA image is ~33 MB of RGB; keep the C heap sane. */
    if (w <= 0 || h <= 0 || (long long)w * h * 3 > 64LL * 1024 * 1024) {
        stbi_image_free(pix);
        PyErr_SetString(PyExc_ValueError, "_img.decode: image too large");
        return NULL;
    }
    PyObject *b = PyBytes_FromStringAndSize((const char *)pix,
                                            (Py_ssize_t)(w * h * 3));
    stbi_image_free(pix);
    if (!b) {
        return NULL;
    }
    return Py_BuildValue("(iiiN)", w, h, 3, b);
}

static PyMethodDef img_methods[] = {
    {"decode", img_decode, METH_VARARGS,
     "decode PNG/JPEG/GIF/BMP bytes to (width, height, 3, rgb-bytes)"},
    {NULL, NULL, 0, NULL}
};

static struct PyModuleDef img_module = {
    PyModuleDef_HEAD_INIT,
    "_img",
    "fast image decode (stb_image)",
    -1,
    img_methods
};

PyMODINIT_FUNC PyInit__img(void) {
    return PyModule_Create(&img_module);
}
