/* h264decmod.c -- _h264 builtin module: H.264/AVC decode via h264bsd.
 *
 * Vendored decoder: h264bsd (Android/Nokia heritage, Apache-2.0) in
 * shim/h264bsd/.  It decodes the H.264 BASELINE profile (the profile used
 * by virtually all small/legacy MP4 files) to YUV420 (I420).
 *
 * API:
 *   _h264.decode(annexb_bytes) -> (w, h, yuv_bytes) | None
 *       Feed one Annex-B access unit (SPS+PPS+slice NALs with 00 00 00 01
 *       start codes).  Returns (coded_width, coded_height, I420) when a
 *       picture completes, else None.  The decoder keeps state between
 *       calls (SPS/PPS, reference frames), so P-frames decode correctly.
 *   _h264.to_rgb(yuv, w, h, dw, dh) -> rgb_bytes
 *       Convert an I420 frame of coded size w x h to RGB24, keeping only
 *       the top-left dw x dh (the coded height is MB-aligned, i.e. the
 *       bottom rows are padding).
 *   _h264.close()
 *       Free the decoder (call when playback ends).
 */

#include <Python.h>
#include <string.h>
#include "h264bsd/h264bsd_decoder.h"
#include "h264bsd/h264bsd_util.h"
#include "h264bsd/h264bsd_storage.h"

static storage_t *g_dec = NULL;

static PyObject *h264_close(PyObject *self, PyObject *args) {
    (void)self;
    (void)args;
    if (g_dec != NULL) {
        h264bsdShutdown(g_dec);
        h264bsdFree(g_dec);
        g_dec = NULL;
    }
    Py_RETURN_NONE;
}

static PyObject *h264_decode(PyObject *self, PyObject *args) {
    (void)self;
    Py_buffer view;
    if (!PyArg_ParseTuple(args, "y*", &view)) {
        return NULL;
    }
    if (view.len <= 0 || view.buf == NULL) {
        PyBuffer_Release(&view);
        PyErr_SetString(PyExc_ValueError, "_h264.decode: empty input");
        return NULL;
    }
    if (g_dec == NULL) {
        g_dec = h264bsdAlloc();
        if (g_dec == NULL) {
            PyBuffer_Release(&view);
            return PyErr_NoMemory();
        }
        if (h264bsdInit(g_dec, 1 /* noOutputReordering */) != HANTRO_OK) {
            h264bsdFree(g_dec);
            g_dec = NULL;
            PyBuffer_Release(&view);
            PyErr_SetString(PyExc_RuntimeError, "_h264.decode: decoder init failed");
            return NULL;
        }
    }
    u8 *pic = NULL;
    u32 w = 0, h = 0;
    u32 ret = h264bsdDecode(g_dec, (u8 *)view.buf, (u32)view.len, &pic, &w, &h);
    PyBuffer_Release(&view);
    if (ret == H264BSD_PIC_RDY) {
        size_t yuv_len = (size_t)w * h * 3 / 2;
        PyObject *b = PyBytes_FromStringAndSize(NULL, (Py_ssize_t)yuv_len);
        if (b == NULL) {
            return NULL;
        }
        if (pic != NULL) {
            memcpy(PyBytes_AS_STRING(b), pic, yuv_len);
        }
        return Py_BuildValue("(iiN)", (int)w, (int)h, b);
    }
    if (ret == H264BSD_RDY || ret == H264BSD_HDRS_RDY) {
        Py_RETURN_NONE; /* no complete picture in this buffer */
    }
    PyErr_Format(PyExc_ValueError, "_h264.decode failed (h264bsd code %u)", ret);
    return NULL;
}

/* BT.601 YUV420 -> RGB24 (integer math), cropping coded (w,h) to (dw,dh). */
static PyObject *h264_to_rgb(PyObject *self, PyObject *args) {
    (void)self;
    Py_buffer view;
    int w, h, dw, dh;
    if (!PyArg_ParseTuple(args, "y*iiii", &view, &w, &h, &dw, &dh)) {
        return NULL;
    }
    if (w <= 0 || h <= 0 || dw <= 0 || dh <= 0 || dw > w || dh > h) {
        PyBuffer_Release(&view);
        PyErr_SetString(PyExc_ValueError, "_h264.to_rgb: bad dimensions");
        return NULL;
    }
    if (view.len < (Py_ssize_t)((size_t)w * h * 3 / 2)) {
        PyBuffer_Release(&view);
        PyErr_SetString(PyExc_ValueError, "_h264.to_rgb: buffer too small");
        return NULL;
    }
    const u8 *y = (const u8 *)view.buf;
    const u8 *u = y + (size_t)w * h;
    const u8 *v = u + (size_t)w * h / 4;
    PyObject *b = PyBytes_FromStringAndSize(NULL, (Py_ssize_t)((size_t)dw * dh * 3));
    if (b == NULL) {
        PyBuffer_Release(&view);
        return NULL;
    }
    u8 *out = (u8 *)PyBytes_AS_STRING(b);
    for (int yy = 0; yy < dh; yy++) {
        const u8 *yr = y + (size_t)yy * w;
        const u8 *ur = u + (size_t)(yy / 2) * (w / 2);
        const u8 *vr = v + (size_t)(yy / 2) * (w / 2);
        u8 *orow = out + (size_t)yy * dw * 3;
        for (int xx = 0; xx < dw; xx++) {
            int c = (int)yr[xx] - 16;
            int d = (int)ur[xx / 2] - 128;
            int e = (int)vr[xx / 2] - 128;
            int r = (298 * c + 409 * e + 128) >> 8;
            int g = (298 * c - 100 * d - 208 * e + 128) >> 8;
            int bl = (298 * c + 516 * d + 128) >> 8;
            if (r < 0) r = 0; else if (r > 255) r = 255;
            if (g < 0) g = 0; else if (g > 255) g = 255;
            if (bl < 0) bl = 0; else if (bl > 255) bl = 255;
            orow[xx * 3] = (u8)r;
            orow[xx * 3 + 1] = (u8)g;
            orow[xx * 3 + 2] = (u8)bl;
        }
    }
    PyBuffer_Release(&view);
    return b;
}

static PyMethodDef h264_methods[] = {
    {"decode", h264_decode, METH_VARARGS,
     "decode(annexb) -> (w, h, yuv) or None: decode one Annex-B H.264 access unit"},
    {"to_rgb", h264_to_rgb, METH_VARARGS,
     "to_rgb(yuv, w, h, dw, dh) -> rgb bytes: YUV420->RGB24 (crop to dw x dh)"},
    {"close", h264_close, METH_NOARGS,
     "close(): free the decoder state"},
    {NULL, NULL, 0, NULL}
};

static struct PyModuleDef h264_module = {
    PyModuleDef_HEAD_INIT,
    "_h264",
    "h264bsd-backed H.264 baseline decoder (M10.1)",
    -1,
    h264_methods
};

PyMODINIT_FUNC PyInit__h264(void) {
    return PyModule_Create(&h264_module);
}
