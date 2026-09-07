/* mp3decmod.c -- M9.5 _mp3dec builtin module: real MP3 -> 16-bit PCM.
 *
 * Wraps the vendored single-header minimp3 decoder (public domain,
 * lieff/minimp3).  The kernel is freestanding (no libc beyond the shim),
 * so the module is compiled with the same zig/musl flags as pyshim.c and
 * registered via PyImport_AppendInittab in pyboot_start().
 *
 * API:  _mp3dec.decode(data) -> (pcm_bytes, sample_rate, channels)
 *   data         raw MP3 bytes (ID3 tags + MPEG frames)
 *   pcm_bytes    16-bit little-endian interleaved PCM (int16 per channel)
 *   sample_rate  Hz of the first audio frame
 *   channels     1 (mono) or 2 (stereo)
 * The caller (mp3.py) downmixes/decimates as needed for the speaker.
 */

#define MINIMP3_IMPLEMENTATION
#include "minimp3.h"

#include <Python.h>
#include <string.h>

/* Cap the decoded PCM so a malformed/huge file cannot exhaust the 96 MiB
   C heap: ~64 MiB of PCM = ~25 minutes of stereo 44.1 kHz. */
#define MAX_PCM_BYTES (64 * 1024 * 1024)

static PyObject *mp3dec_decode(PyObject *self, PyObject *args) {
    (void)self;
    Py_buffer view;
    if (!PyArg_ParseTuple(args, "y*", &view)) {
        return NULL;
    }
    const uint8_t *src = (const uint8_t *)view.buf;
    Py_ssize_t src_len = view.len;
    if (src_len <= 0 || src == NULL) {
        PyBuffer_Release(&view);
        PyErr_SetString(PyExc_ValueError, "_mp3dec.decode: empty input");
        return NULL;
    }

    /* Two-pass decode: minimp3 must write into a real per-frame
       buffer, but growing via realloc() doubles peak memory (the
       kernel C heap is only 96 MiB and a 2.7 MB MP3 yields ~20 MB
       of PCM).  Pass 1 walks frames with a scratch buffer and
       counts the exact sample total; pass 2 decodes into one
       exact allocation. */
    mp3dec_t dec;
    mp3dec_init(&dec);
    static int16_t frame_buf[MINIMP3_MAX_SAMPLES_PER_FRAME * 2];

    /* Pass 1: count. */
    size_t pos = 0;
    size_t total_samples = 0;
    int rate = 0;
    int channels = 0;
    while (pos < (size_t)src_len) {
        mp3dec_frame_info_t info;
        memset(&info, 0, sizeof(info));
        int nsamples = mp3dec_decode_frame(&dec, src + pos,
                                           (int)(src_len - pos),
                                           frame_buf, &info);
        if (nsamples <= 0 || info.frame_bytes <= 0) {
            break;
        }
        if (info.channels < 1 || info.channels > 2 || info.hz <= 0) {
            break;
        }
        if (rate == 0) {
            rate = info.hz;
            channels = info.channels;
        }
        total_samples += (size_t)nsamples * (size_t)info.channels;
        pos += (size_t)info.frame_bytes;
        if (total_samples > MAX_PCM_BYTES / 2) {
            break; /* cap */
        }
    }
    if (total_samples == 0 || rate == 0) {
        PyBuffer_Release(&view);
        PyErr_SetString(PyExc_ValueError, "_mp3dec.decode: no MPEG audio found");
        return NULL;
    }

    /* Pass 2: decode into the exact-size buffer. */
    size_t total_bytes = total_samples * 2;
    uint8_t *pcm = (uint8_t *)malloc(total_bytes);
    if (pcm == NULL) {
        PyBuffer_Release(&view);
        return PyErr_NoMemory();
    }
    mp3dec_init(&dec);
    pos = 0;
    size_t used = 0;
    while (pos < (size_t)src_len && used < total_bytes) {
        mp3dec_frame_info_t info;
        memset(&info, 0, sizeof(info));
        int nsamples = mp3dec_decode_frame(&dec, src + pos,
                                           (int)(src_len - pos),
                                           frame_buf, &info);
        if (nsamples <= 0 || info.frame_bytes <= 0) {
            break;
        }
        size_t bytes = (size_t)nsamples * (size_t)info.channels * 2;
        if (used + bytes > total_bytes) {
            bytes = total_bytes - used;
        }
        memcpy(pcm + used, frame_buf, bytes);
        used += bytes;
        pos += (size_t)info.frame_bytes;
    }
    PyBuffer_Release(&view);

    PyObject *b = PyBytes_FromStringAndSize((const char *)pcm, (Py_ssize_t)used);
    free(pcm);
    if (b == NULL) {
        return NULL;
    }
    PyObject *r = Py_BuildValue("(Nii)", b, rate, channels);
    if (r == NULL) {
        Py_DECREF(b);
    }
    return r;
}



/* mono8k: downmix interleaved int16 PCM to mono and decimate toward
   8000 Hz in one C pass, so the pure-Python tone tracker stays fast.
   pcm is the bytes object from decode(); returns (mono_pcm_bytes, out_rate).
   out_rate = min(rate, 8000) (nearest-sample decimation, channels averaged). */
static PyObject *mp3dec_mono8k(PyObject *self, PyObject *args) {
    (void)self;
    Py_buffer view;
    int rate = 0;
    int channels = 0;
    if (!PyArg_ParseTuple(args, "y*ii", &view, &rate, &channels)) {
        return NULL;
    }
    if (channels < 1 || channels > 2 || rate <= 0) {
        PyBuffer_Release(&view);
        PyErr_SetString(PyExc_ValueError, "_mp3dec.mono8k: bad rate/channels");
        return NULL;
    }
    const int16_t *src = (const int16_t *)view.buf;
    Py_ssize_t nframes = view.len / (2 * channels);
    int out_rate = rate < 8000 ? rate : 8000;
    int step = (rate + out_rate - 1) / out_rate;
    if (step < 1) { step = 1; }
    Py_ssize_t out_n = (nframes + step - 1) / step;
    if (out_n > 8 * 1024 * 1024) {
        out_n = 8 * 1024 * 1024;
    }
    PyObject *b = PyBytes_FromStringAndSize(NULL, out_n * 2);
    if (b == NULL) {
        PyBuffer_Release(&view);
        return NULL;
    }
    int16_t *dst = (int16_t *)PyBytes_AS_STRING(b);
    Py_ssize_t o = 0;
    for (Py_ssize_t i = 0; i < nframes && o < out_n; i += step, o++) {
        int s = src[i * channels];
        if (channels == 2) {
            s = (s + src[i * channels + 1]) >> 1;
        }
        dst[o] = (int16_t)s;
    }
    PyBuffer_Release(&view);
    PyObject *r = Py_BuildValue("(Ni)", b, out_rate);
    if (r == NULL) {
        Py_DECREF(b);
    }
    return r;
}

static PyMethodDef mp3dec_methods[] = {
    {"decode", mp3dec_decode, METH_VARARGS,
     "decode(data) -> (pcm_bytes, sample_rate, channels): decode an MP3"
     " buffer to 16-bit little-endian interleaved PCM"},
    {"mono8k", mp3dec_mono8k, METH_VARARGS,
     "mono8k(pcm, rate, channels) -> (mono_pcm, out_rate): downmix to"
     " mono and decimate toward 8000 Hz"},
    {NULL, NULL, 0, NULL}
};


static struct PyModuleDef mp3dec_module = {
    PyModuleDef_HEAD_INIT,
    "_mp3dec",
    "minimp3-backed MP3 decoder (M9.5)",
    -1,
    mp3dec_methods
};

PyMODINIT_FUNC PyInit__mp3dec(void) {
    return PyModule_Create(&mp3dec_module);
}