/* pyshim.c -- LP64 musl-ABI C shim for the embedded CPython kernel build.
 *
 * Provides the variadic printf family (which Rust cannot define), a minimal
 * FILE layer (no filesystem yet; stdout/stderr go to the serial console),
 * and the Python bootstrap glue (PyConfig setup).
 *
 * Compiled with `zig cc -target x86_64-linux-musl` to match the CPython
 * objects (SysV ABI, LP64).  Self-contained: performs its own port I/O,
 * no libc, no Rust calls.
 */

#include <stdarg.h>
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <errno.h>
#include <unistd.h>
#include <pthread.h>

/* ---- serial console (COM1) ---- */

static inline unsigned char inb(unsigned short port) {
    unsigned char v;
    __asm__ volatile("inb %1, %0" : "=a"(v) : "d"(port));
    return v;
}

static inline void outb(unsigned short port, unsigned char v) {
    __asm__ volatile("outb %0, %1" : : "a"(v), "d"(port));
}

static void serial_putc(char c) {
    while ((inb(0x3F8 + 5) & 0x20) == 0) {
    }
    outb(0x3F8, (unsigned char)c);
}

static void serial_write(const char *s, size_t n) {
    for (size_t i = 0; i < n; i++) {
        serial_putc(s[i]);
    }
}

/* ---- FILE layer ----
 *
 * musl's FILE is an opaque struct (only forward-declared in stdio.h), so we
 * allocate raw storage and never touch the internals.  We identify the three
 * standard streams by pointer identity.  CPython references the
 * `stdin`/`stdout`/`stderr` globals directly (musl style); the stdio.h
 * macros must be removed before redefining the symbols. */

#undef stdin
#undef stdout
#undef stderr

static _Alignas(16) unsigned char iob_storage[3 * 512];
FILE *const stdin = (FILE *)&iob_storage[0];
FILE *const stdout = (FILE *)&iob_storage[512];
FILE *const stderr = (FILE *)&iob_storage[1024];

static int fd_of(FILE *f) {
    if (f == stdout) {
        return 1;
    }
    if (f == stderr) {
        return 2;
    }
    if (f == stdin) {
        return 0;
    }
    return -1;
}

/* ---- mini printf ---- */

typedef struct Out {
    char *buf;     /* NULL = count only */
    size_t n;      /* buffer capacity */
    size_t pos;    /* bytes written */
    int count_only;
} Out;

static void out_putc(Out *o, char c) {
    if (!o->count_only) {
        if (o->pos < o->n - 1) {
            o->buf[o->pos] = c;
        }
    }
    o->pos++;
}

static void out_puts(Out *o, const char *s, size_t len) {
    if (!o->count_only) {
        size_t room = o->n > 0 ? o->n - 1 - o->pos : 0;
        size_t w = len < room ? len : room;
        for (size_t i = 0; i < w; i++) {
            o->buf[o->pos + i] = s[i];
        }
    }
    o->pos += len;
}

static void out_pad(Out *o, char c, size_t n) {
    for (size_t i = 0; i < n; i++) {
        out_putc(o, c);
    }
}

static void out_unsigned(Out *o, unsigned long long v, int base, int upper,
                         int width, char pad, int plus, int space) {
    char digits[70];
    int n = 0;
    if (v == 0) {
        digits[n++] = '0';
    } else {
        while (v) {
            int d = (int)(v % (unsigned)base);
            digits[n++] = d < 10 ? (char)('0' + d)
                                 : (char)((upper ? 'A' : 'a') + d - 10);
            v /= (unsigned)base;
        }
    }
    int sig = 0;
    if (plus) {
        sig = 1;
    } else if (space) {
        sig = 1;
    }
    int padn = width - n - sig;
    if (padn < 0) {
        padn = 0;
    }
    if (pad != '0') {
        out_pad(o, ' ', padn);
    }
    if (plus) {
        out_putc(o, '+');
    } else if (space) {
        out_putc(o, ' ');
    }
    if (pad == '0') {
        out_pad(o, '0', padn);
    }
    for (int i = n - 1; i >= 0; i--) {
        out_putc(o, digits[i]);
    }
}

static void out_signed(Out *o, long long v, int width, char pad, int plus,
                       int space) {
    if (v < 0) {
        out_putc(o, '-');
        out_unsigned(o, (unsigned long long)(-(v + 1)) + 1, 10, 0, width - 1,
                     pad, 0, 0);
    } else {
        out_unsigned(o, (unsigned long long)v, 10, 0, width, pad, plus, space);
    }
}

static void out_string(Out *o, const char *s, int width, int left, int prec) {
    size_t len = 0;
    while (s[len] && (prec < 0 || (int)len < prec)) {
        len++;
    }
    if (!left && width > (int)len) {
        out_pad(o, ' ', width - (int)len);
    }
    out_puts(o, s, len);
    if (left && width > (int)len) {
        out_pad(o, ' ', width - (int)len);
    }
}

static void out_wide(Out *o, const unsigned int *s, int width, int left,
                     int prec) {
    char buf[512];
    size_t len = 0;
    for (size_t i = 0; i < 256 && len < sizeof(buf) - 3; i++) {
        unsigned c = s[i];
        if (!c) {
            break;
        }
        if (prec >= 0 && (int)len >= prec) {
            break;
        }
        if (c < 0x80) {
            buf[len++] = (char)c;
        } else if (c < 0x800) {
            buf[len++] = (char)(0xC0 | (c >> 6));
            buf[len++] = (char)(0x80 | (c & 0x3F));
        } else if (c < 0x10000) {
            buf[len++] = (char)(0xE0 | (c >> 12));
            buf[len++] = (char)(0x80 | ((c >> 6) & 0x3F));
            buf[len++] = (char)(0x80 | (c & 0x3F));
        } else {
            buf[len++] = (char)(0xF0 | (c >> 18));
            buf[len++] = (char)(0x80 | ((c >> 12) & 0x3F));
            buf[len++] = (char)(0x80 | ((c >> 6) & 0x3F));
            buf[len++] = (char)(0x80 | (c & 0x3F));
        }
    }
    out_string(o, buf, width, left, (int)len);
}

/* float formatting helpers */
static int snprintf_help(char *buf, size_t n, const char *fmt, ...);

static unsigned long long round_frac(double *vp, int prec) {
    double v = *vp;
    unsigned long long mul = 1;
    for (int i = 0; i < prec; i++) {
        mul *= 10;
    }
    double r = v * (double)mul;
    unsigned long long iv = (unsigned long long)(r + 0.5);
    /* iv = rounded value in units of 10^-prec.  The integer part is
       iv / mul (carry included), the fraction digits are iv % mul.
       The old check `iv >= mul` fired for ANY value >= 1.0, so e.g.
       sqrt(2) printed as 2.0000. */
    *vp = (double)(iv / mul);
    return iv % mul;
}

static void out_float(Out *o, double v, int prec, char fmt, int width,
                      char pad, int left, int plus, int space, int alt) {
    if (v != v) {
        out_string(o, "nan", width, left, -1);
        return;
    }
    if (v > 1e308) {
        out_string(o, "inf", width, left, -1);
        return;
    }
    if (v < -1e308) {
        out_string(o, "-inf", width, left, -1);
        return;
    }
    if (prec < 0) {
        prec = 6;
    }
    char body[512];
    size_t blen = 0;
    int neg = v < 0;
    if (neg) {
        v = -v;
    }
    if (fmt == 'e' || fmt == 'E' || (fmt == 'g' || fmt == 'G')) {
        int exp10 = 0;
        double mant = v;
        if (v != 0) {
            while (mant >= 10.0) {
                mant /= 10.0;
                exp10++;
            }
            while (mant < 1.0) {
                mant *= 10.0;
                exp10--;
            }
        }
        if (fmt == 'g' || fmt == 'G') {
            if (exp10 < -4 || exp10 >= prec) {
                fmt = (fmt == 'G') ? 'E' : 'e';
            } else {
                int newprec = prec - 1 - exp10;
                if (newprec < 0) {
                    newprec = 0;
                }
                double saved = v;
                unsigned long long frac = round_frac(&v, newprec);
                if (v != saved) {
                    mant = v;
                    exp10 = 0;
                    while (mant >= 10.0) {
                        mant /= 10.0;
                        exp10++;
                    }
                    while (mant < 1.0) {
                        mant *= 10.0;
                        exp10--;
                    }
                }
                unsigned long long ip = (unsigned long long)v;
                if (ip == 0 && (int)frac == 0 && newprec == 0) {
                    blen += (size_t)snprintf_help(body + blen, sizeof(body) - blen, "0");
                } else {
                    blen += (size_t)snprintf_help(body + blen, sizeof(body) - blen, "%llu", ip);
                    if (newprec > 0) {
                        body[blen++] = '.';
                        char tmp[64];
                        int tn = 0;
                        unsigned long long f = frac;
                        for (int i = 0; i < newprec; i++) {
                            tmp[tn++] = (char)('0' + f % 10);
                            f /= 10;
                        }
                        while (tn > 0) {
                            body[blen++] = tmp[--tn];
                        }
                        while (blen > 0 && body[blen - 1] == '0' && !alt) {
                            blen--;
                        }
                        if (blen > 0 && body[blen - 1] == '.' && !alt) {
                            blen--;
                        }
                    }
                }
                goto emit;
            }
        }
        if (fmt == 'e' || fmt == 'E') {
            int ep = prec;
            unsigned long long frac = round_frac(&v, ep);
            if (v >= 10.0) {
                v /= 10.0;
                exp10++;
            }
            unsigned long long ip = (unsigned long long)v;
            blen += (size_t)snprintf_help(body + blen, sizeof(body) - blen, "%llu", ip);
            if (ep > 0) {
                body[blen++] = '.';
                char tmp[64];
                int tn = 0;
                unsigned long long f = frac;
                for (int i = 0; i < ep; i++) {
                    tmp[tn++] = (char)('0' + f % 10);
                    f /= 10;
                }
                while (tn > 0) {
                    body[blen++] = tmp[--tn];
                }
            }
            body[blen++] = (char)(fmt == 'E' ? 'E' : 'e');
            {
                int e = exp10 < 0 ? -exp10 : exp10;
                body[blen++] = exp10 < 0 ? '-' : '+';
                body[blen++] = (char)('0' + (e / 10) % 10);
                body[blen++] = (char)('0' + e % 10);
            }
        }
    } else {
        unsigned long long frac = round_frac(&v, prec);
        unsigned long long ip = (unsigned long long)v;
        blen += (size_t)snprintf_help(body + blen, sizeof(body) - blen, "%llu", ip);
        if (prec > 0) {
            body[blen++] = '.';
            char tmp[64];
            int tn = 0;
            unsigned long long f = frac;
            for (int i = 0; i < prec; i++) {
                tmp[tn++] = (char)('0' + f % 10);
                f /= 10;
            }
            while (tn > 0) {
                body[blen++] = tmp[--tn];
            }
        } else if (alt) {
            body[blen++] = '.';
        }
    }
emit:
    int sig = neg || plus || space;
    int total = (int)blen + sig;
    if (!left && width > total && pad == '0') {
        if (sig) {
            out_putc(o, neg ? '-' : (plus ? '+' : ' '));
            sig = 0;
        }
        out_pad(o, '0', width - total);
    } else if (!left && width > total) {
        out_pad(o, ' ', width - total);
    }
    if (sig) {
        out_putc(o, neg ? '-' : (plus ? '+' : ' '));
    }
    out_puts(o, body, blen);
    if (left && width > total) {
        out_pad(o, ' ', width - total);
    }
}

static void vsnprintf_core(Out *o, const char *fmt, va_list ap) {
    while (*fmt) {
        char c = *fmt++;
        if (c != '%') {
            out_putc(o, c);
            continue;
        }
        int left = 0, plus = 0, space = 0, alt = 0, zero = 0;
        for (;;) {
            c = *fmt;
            if (c == '-') {
                left = 1;
            } else if (c == '+') {
                plus = 1;
            } else if (c == ' ') {
                space = 1;
            } else if (c == '#') {
                alt = 1;
            } else if (c == '0') {
                zero = 1;
            } else {
                break;
            }
            fmt++;
        }
        int width = 0;
        if (*fmt == '*') {
            width = va_arg(ap, int);
            fmt++;
        } else {
            while (*fmt >= '0' && *fmt <= '9') {
                width = width * 10 + (*fmt - '0');
                fmt++;
            }
        }
        int prec = -1;
        if (*fmt == '.') {
            fmt++;
            prec = 0;
            if (*fmt == '*') {
                prec = va_arg(ap, int);
                fmt++;
            } else {
                while (*fmt >= '0' && *fmt <= '9') {
                    prec = prec * 10 + (*fmt - '0');
                    fmt++;
                }
            }
        }
        int len = 0;
        if (*fmt == 'h') {
            fmt++;
            if (*fmt == 'h') {
                fmt++;
            }
            len = 1;
        } else if (*fmt == 'l') {
            fmt++;
            if (*fmt == 'l') {
                fmt++;
                len = 3;
            } else {
                len = 2;
            }
        } else if (*fmt == 'z' || *fmt == 't') {
            fmt++;
            len = 3;
        } else if (*fmt == 'j') {
            fmt++;
            len = 3;
        }
        c = *fmt++;
        char pad = (zero && !left) ? '0' : ' ';
        switch (c) {
        case 'd':
        case 'i': {
            long long v;
            if (len >= 3) {
                v = va_arg(ap, long long);
            } else {
                v = va_arg(ap, int);
            }
            out_signed(o, v, width, pad, plus, space);
            break;
        }
        case 'u':
        case 'o':
        case 'x':
        case 'X': {
            unsigned long long v;
            if (len >= 3) {
                v = va_arg(ap, unsigned long long);
            } else {
                v = va_arg(ap, unsigned int);
            }
            int base = c == 'u' ? 10 : (c == 'o' ? 8 : 16);
            int upper = (c == 'X');
            if (alt && v != 0 && base == 16) {
                out_puts(o, upper ? "0X" : "0x", 2);
                width -= 2;
                if (width < 0) {
                    width = 0;
                }
            }
            out_unsigned(o, v, base, upper, width, pad, 0, 0);
            break;
        }
        case 'c':
            out_putc(o, (char)va_arg(ap, int));
            break;
        case 's': {
            const char *s = va_arg(ap, const char *);
            if (!s) {
                s = "(null)";
            }
            out_string(o, s, width, left, prec);
            break;
        }
        case 'S': {
            const unsigned int *s = va_arg(ap, const unsigned int *);
            if (!s) {
                s = (const unsigned int *)L"(null)";
            }
            out_wide(o, s, width, left, prec);
            break;
        }
        case 'p': {
            void *p = va_arg(ap, void *);
            out_puts(o, "0x", 2);
            out_unsigned(o, (unsigned long long)(uintptr_t)p, 16, 0, 0, 0, 0,
                         0);
            break;
        }
        case 'f':
        case 'F':
        case 'e':
        case 'E':
        case 'g':
        case 'G': {
            double v = va_arg(ap, double);
            out_float(o, v, prec, c, width, pad, left, plus, space, alt);
            break;
        }
        case '%':
            out_putc(o, '%');
            break;
        default:
            out_putc(o, '%');
            out_putc(o, c);
            break;
        }
    }
}

static int vsnprintf_shim(char *buf, size_t n, const char *fmt, va_list ap) {
    Out o = {buf, n, 0, 0};
    vsnprintf_core(&o, fmt, ap);
    if (n > 0) {
        if (o.pos < n) {
            buf[o.pos] = 0;
        } else {
            buf[n - 1] = 0;
        }
    }
    return (int)o.pos;
}

static int snprintf_help(char *buf, size_t n, const char *fmt, ...) {
    va_list ap;
    va_start(ap, fmt);
    int r = vsnprintf_shim(buf, n, fmt, ap);
    va_end(ap);
    return r;
}

/* ---- printf family (plain musl names) ---- */

int vsnprintf(char *buf, size_t n, const char *fmt, va_list ap) {
    return vsnprintf_shim(buf, n, fmt, ap);
}

int snprintf(char *buf, size_t n, const char *fmt, ...) {
    va_list ap;
    va_start(ap, fmt);
    int r = vsnprintf_shim(buf, n, fmt, ap);
    va_end(ap);
    return r;
}

int sprintf(char *buf, const char *fmt, ...) {
    va_list ap;
    va_start(ap, fmt);
    int r = vsnprintf_shim(buf, (size_t)-1, fmt, ap);
    va_end(ap);
    return r;
}

int vsprintf(char *buf, const char *fmt, va_list ap) {
    return vsnprintf_shim(buf, (size_t)-1, fmt, ap);
}

int printf(const char *fmt, ...) {
    va_list ap;
    va_start(ap, fmt);
    static char tmp[2048];
    int r = vsnprintf_shim(tmp, sizeof(tmp), fmt, ap);
    va_end(ap);
    serial_write(tmp, (size_t)r < sizeof(tmp) ? (size_t)r : sizeof(tmp));
    return r;
}

int vfprintf(FILE *f, const char *fmt, va_list ap) {
    va_list ap2;
    va_copy(ap2, ap);
    static char tmp[2048];
    int r = vsnprintf_shim(tmp, sizeof(tmp), fmt, ap2);
    va_end(ap2);
    int fd = fd_of(f);
    if (fd == 1 || fd == 2) {
        serial_write(tmp, (size_t)r < sizeof(tmp) ? (size_t)r : sizeof(tmp));
    }
    return r;
}

int fprintf(FILE *f, const char *fmt, ...) {
    va_list ap;
    va_start(ap, fmt);
    int r = vfprintf(f, fmt, ap);
    va_end(ap);
    return r;
}

int vprintf(const char *fmt, va_list ap) {
    return vfprintf(stdout, fmt, ap);
}

/* ---- character/stream stubs ----
 *
 * Regular files are served by the kernel's initramfs VFS through the fd
 * layer (read/close/lseek come from musl's unistd.h).  A small pool maps
 * FILE* to those fds. */

extern int kern_open(const char *path, int flags);

#define VFILE_POOL 16
static unsigned char vfile_storage[VFILE_POOL][64];
static int vfile_fd[VFILE_POOL];
static int vfile_used[VFILE_POOL];
static int vfile_eof[VFILE_POOL];
/* pushback stack for getc/ungetc (CPython's tokenizer reads FILE sources
   via getc()+ungetc() lookahead; a small LIFO is enough) */
static int vfile_pb[VFILE_POOL][8];
static int vfile_pbn[VFILE_POOL];

static int vfile_index(FILE *f) {
    for (int i = 0; i < VFILE_POOL; i++) {
        if (vfile_used[i] && (FILE *)vfile_storage[i] == f) {
            return i;
        }
    }
    return -1;
}

int putchar(int c) {
    serial_putc((char)c);
    return c;
}

int fputc(int c, FILE *f) {
    int fd = fd_of(f);
    if (fd == 1 || fd == 2) {
        serial_putc((char)c);
        return c;
    }
    return -1;
}

int fputs(const char *s, FILE *f) {
    size_t n = 0;
    while (s[n]) {
        n++;
    }
    int fd = fd_of(f);
    if (fd == 1 || fd == 2) {
        serial_write(s, n);
        return 0;
    }
    return -1;
}

int puts(const char *s) {
    size_t n = 0;
    while (s[n]) {
        n++;
    }
    serial_write(s, n);
    serial_putc('\n');
    return 0;
}

int fflush(FILE *f) {
    (void)f;
    return 0;
}

int ferror(FILE *f) {
    (void)f;
    return 0;
}

void clearerr(FILE *f) {
    (void)f;
}

int feof(FILE *f) {
    int i = vfile_index(f);
    return i >= 0 ? vfile_eof[i] : 0;
}

void rewind(FILE *f) {
    int i = vfile_index(f);
    if (i >= 0) {
        lseek(vfile_fd[i], 0, 0);
        vfile_pbn[i] = 0;
        vfile_eof[i] = 0;
    }
}

int ungetc(int c, FILE *f) {
    int i = vfile_index(f);
    if (i < 0 || c < 0) {
        return -1;
    }
    if (vfile_pbn[i] >= 8) {
        return -1;
    }
    vfile_pb[i][vfile_pbn[i]++] = c;
    vfile_eof[i] = 0;
    return c;
}

int fileno(FILE *f) {
    int fd = fd_of(f);
    if (fd >= 0) {
        return fd;
    }
    int i = vfile_index(f);
    return i >= 0 ? vfile_fd[i] : -1;
}

int setvbuf(FILE *f, char *buf, int mode, size_t size) {
    (void)f;
    (void)buf;
    (void)mode;
    (void)size;
    return 0;
}

FILE *fdopen(int fd, const char *mode) {
    (void)mode;
    if (fd == 1) {
        return stdout;
    }
    if (fd == 2) {
        return stderr;
    }
    if (fd == 0) {
        return stdin;
    }
    return 0;
}

/* Map a fopen(3) mode string to open(2) flags (musl values). */
static int fopen_flags(const char *mode) {
    if (!mode || !*mode) {
        return 0;
    }
    int acc = 0;
    int flags = 0;
    switch (mode[0]) {
        case 'w':
            acc = 1; /* O_WRONLY */
            flags |= 0x40 | 0x200; /* O_CREAT | O_TRUNC */
            break;
        case 'a':
            acc = 1;
            flags |= 0x40 | 0x400; /* O_CREAT | O_APPEND */
            break;
        default: /* 'r' and anything else: read-only */
            acc = 0;
            break;
    }
    for (const char *m = mode + 1; *m; m++) {
        if (*m == '+') {
            acc = 2; /* O_RDWR */
        }
    }
    return acc | flags;
}

FILE *fopen(const char *path, const char *mode) {
    int fd = kern_open(path, fopen_flags(mode));
    if (fd < 0) {
        return 0; /* kern_open set errno */
    }
    for (int i = 0; i < VFILE_POOL; i++) {
        if (!vfile_used[i]) {
            vfile_used[i] = 1;
            vfile_fd[i] = fd;
            vfile_eof[i] = 0;
            vfile_pbn[i] = 0;
            return (FILE *)&vfile_storage[i];
        }
    }
    close(fd);
    errno = 24; /* EMFILE */
    return 0;
}

int fclose(FILE *f) {
    int i = vfile_index(f);
    if (i < 0) {
        return 0;
    }
    close(vfile_fd[i]);
    vfile_used[i] = 0;
    vfile_pbn[i] = 0;
    return 0;
}

size_t fread(void *ptr, size_t size, size_t nmemb, FILE *f) {
    int i = vfile_index(f);
    if (i < 0 || size == 0) {
        return 0;
    }
    size_t want = size * nmemb;
    long got = read(vfile_fd[i], ptr, want);
    if (got < 0) {
        return 0;
    }
    if ((size_t)got < want) {
        vfile_eof[i] = 1;
    }
    return (size_t)got / size;
}

size_t fwrite(const void *ptr, size_t size, size_t nmemb, FILE *f) {
    int fd = fd_of(f);
    if (fd == 1 || fd == 2) {
        serial_write((const char *)ptr, size * nmemb);
        return nmemb;
    }
    int i = vfile_index(f);
    if (i >= 0) {
        long r = write(vfile_fd[i], ptr, size * nmemb);
        if (r > 0) {
            return (size_t)r / size;
        }
    }
    return 0;
}

char *fgets(char *s, int n, FILE *f) {
    int i = vfile_index(f);
    if (i < 0 || n <= 0) {
        return 0;
    }
    int k = 0;
    while (k < n - 1) {
        char c;
        if (vfile_pbn[i] > 0) {
            c = (char)vfile_pb[i][--vfile_pbn[i]];
        } else {
            long r = read(vfile_fd[i], &c, 1);
            if (r != 1) {
                if (r < 0) {
                    return 0;
                }
                vfile_eof[i] = 1;
                if (k == 0) {
                    return 0;
                }
                break;
            }
        }
        s[k++] = c;
        if (c == '\n') {
            break;
        }
    }
    s[k] = 0;
    return s;
}

int fgetc(FILE *f) {
    int i = vfile_index(f);
    if (i < 0) {
        return -1;
    }
    if (vfile_pbn[i] > 0) {
        return vfile_pb[i][--vfile_pbn[i]];
    }
    unsigned char c;
    long r = read(vfile_fd[i], &c, 1);
    if (r == 1) {
        return c;
    }
    vfile_eof[i] = 1;
    return -1;
}

int getc(FILE *f) {
    return fgetc(f);
}

int getc_unlocked(FILE *f) {
    return fgetc(f);
}

long ftell(FILE *f) {
    int i = vfile_index(f);
    if (i < 0) {
        return -1;
    }
    return (long)lseek(vfile_fd[i], 0, 1);
}

void flockfile(FILE *f) {
    (void)f;
}

void funlockfile(FILE *f) {
    (void)f;
}

void perror(const char *s) {
    if (s && *s) {
        serial_write(s, strlen(s));
        serial_write(": ", 2);
    }
    serial_write("Unknown error\n", 14);
}

/* ---- variadic file functions (Rust cannot define variadics) ---- */

#define FD_URANDOM 100
#define FD_STDOUT 1
#define FD_STDERR 2

int open(const char *path, int flags, ...) {
    return kern_open(path, flags);
}

int openat(int dirfd, const char *path, int flags, ...) {
    (void)dirfd;
    return open(path, flags);
}

int ioctl(int fd, unsigned long request, ...) {
    (void)fd;
    (void)request;
    errno = ENOTTY;
    return -1;
}

int fcntl(int fd, int cmd, ...) {
    (void)fd;
    switch (cmd) {
    case 1: /* F_GETFD */
    case 3: /* F_GETFL */
    case 2: /* F_SETFD */
    case 4: /* F_SETFL */
        return 0;
    default:
        errno = EINVAL;
        return -1;
    }
}

long syscall(long number, ...) {
    (void)number;
    errno = ENOSYS;
    return -1;
}

/* ---- Python bootstrap glue ---- */

#include <Python.h>

static void pyboot_probe(char c) {
    __asm__ __volatile__("mov $0x3F8, %%dx\n\tmov %0, %%al\n\tout %%al, %%dx" : : "r"(c) : "rax", "rdx");
}

static void _pyboot_fail(PyStatus status) {
    Py_ExitStatusException(status);
}

PyMODINIT_FUNC PyInit_kern(void); /* defined below */

/* M9.1 format modules: compiled into the kernel image by kernel/build.rs
   (zlib/bzip2/liblzma/expat are vendored next to the kernel tree). */
extern PyObject *PyInit_zlib(void);
extern PyObject *PyInit__bz2(void);
extern PyObject *PyInit__lzma(void);
extern PyObject *PyInit__elementtree(void);
extern PyObject *PyInit_pyexpat(void);
extern PyObject *PyInit__mp3dec(void);
extern PyObject *PyInit__img(void);
extern PyObject *PyInit__h264(void);

static void pyboot_add_inittab(const char *name, PyObject *(*initfunc)(void)) {
    if (PyImport_AppendInittab(name, initfunc) == -1) {
        _pyboot_fail(PyStatus_Error("cannot add inittab entry"));
    }
}

void pyboot_start(void) {
    pyboot_probe('a');
    if (PyImport_AppendInittab("kern", &PyInit_kern) == -1) {
        _pyboot_fail(PyStatus_Error("cannot add kern inittab"));
    }
    pyboot_add_inittab("zlib", &PyInit_zlib);
    pyboot_add_inittab("_bz2", &PyInit__bz2);
    pyboot_add_inittab("_lzma", &PyInit__lzma);
    pyboot_add_inittab("_elementtree", &PyInit__elementtree);
    pyboot_add_inittab("pyexpat", &PyInit_pyexpat);
    pyboot_add_inittab("_mp3dec", &PyInit__mp3dec);
    pyboot_add_inittab("_img", &PyInit__img);
    pyboot_add_inittab("_h264", &PyInit__h264);
    PyConfig config;
    PyConfig_InitIsolatedConfig(&config);
    pyboot_probe('b');

    config.site_import = 0;
    config.pathconfig_warnings = 0;
    config.use_hash_seed = 1;
    config.hash_seed = 0;
    config._install_importlib = 1;

    /* Force UTF-8 filesystem encoding (no locale machinery available). */
    PyConfig_SetBytesString(&config, &config.filesystem_encoding, "utf-8");
    pyboot_probe('c');
    PyConfig_SetBytesString(&config, &config.filesystem_errors, "surrogateescape");
    pyboot_probe('d');
    PyConfig_SetBytesString(&config, &config.stdio_encoding, "utf-8");
    pyboot_probe('e');
    PyConfig_SetBytesString(&config, &config.stdio_errors, "backslashreplace");
    pyboot_probe('f');

    /* Everything comes from frozen modules plus the initramfs stdlib. */
    config.module_search_paths_set = 1;
    PyWideStringList_Append(&config.module_search_paths, L"");
    PyWideStringList_Append(&config.module_search_paths, L"/");
    pyboot_probe('d');

    PyStatus status = PyConfig_SetBytesString(&config, &config.program_name,
                                              "kernel-python");
    if (PyStatus_Exception(status)) {
        _pyboot_fail(status);
    }
    pyboot_probe('e');

    status = Py_InitializeFromConfig(&config);
    pyboot_probe('f');
    PyConfig_Clear(&config);
    if (PyStatus_Exception(status)) {
        _pyboot_fail(status);
    }
}

/* ---- `kern` builtin module: console, timers, memory ----
 *
 * Exposes OS services to Python.  Timer callbacks are invoked from the
 * kernel main loop (kern_process_timers), never from interrupt context. */

extern unsigned long long kern_tick_ms(void);
extern unsigned long long kern_alloc_used(void);
extern unsigned long long kern_alloc_total(void);
extern int sched_yield(void);

/* M6: framebuffer (published by the kernel from BootInfo; the bootloader
   maps it, Python draws into it through a zero-copy memoryview). */
extern int kern_fb_present(void);
extern void kern_fb_get(unsigned *w, unsigned *h, unsigned *stride,
                        unsigned *bpp, int *format);
extern void *kern_fb_addr(void);
extern unsigned long long kern_fb_len(void);

/* Keyboard events (kernel-side decode; struct must match keyboard.rs). */
struct kern_key_evt {
    const char *name;
    unsigned char ch;
    unsigned char pressed;
};
extern int kern_key_event_pop(struct kern_key_evt *out);

static PyObject *key_callback = NULL;

/* Mouse events (kernel-side packet decode; struct must match mouse.rs).
   dx/dy are signed pixel deltas (positive y is down); buttons is a bitmask
   (1=left, 2=right, 4=middle); wheel is the signed wheel delta (0 when the
   wheel is not enabled). */
struct kern_mouse_evt {
    short dx;
    short dy;
    unsigned char buttons;
    short wheel;
};
extern int kern_mouse_event_pop(struct kern_mouse_evt *out);

static PyObject *mouse_callback = NULL;

/* Runtime display resolution change (Bochs VBE on QEMU stdvga); returns 1
   and updates the kernel geometry on success, 0 otherwise. */
extern int kern_fb_set_mode(unsigned w, unsigned h);

/* M9.5: PC speaker (PIT channel 2 + port 0x61).  kern_speaker_freq(0)
   silences the speaker; a nonzero hz programs the square-wave tone. */
extern void kern_speaker_freq(unsigned hz);
extern void kern_speaker_off(void);

/* M9.7: AC'97 audio (kern.audio_*).  kern_audio_play pushes 16-bit LE PCM
   (mono or stereo) into the kernel DMA ring; volume is 0..128. */
/* Phase 2: e1000 + smoltcp (kern.net_*).  Sockets are a small fixed
   pool in the kernel; all calls are non-blocking (kern_net_poll waits). */
extern int kern_net_ready(void);
extern unsigned int kern_net_ip(void);
extern int kern_net_mac(unsigned char *out);
extern int kern_net_socket(int is_tcp);
extern int kern_net_connect(int fd, unsigned int ip, unsigned short port);
extern long long kern_net_send(int fd, const unsigned char *buf, long long len);
extern long long kern_net_recv(int fd, unsigned char *buf, long long cap);
extern int kern_net_poll(int fd, long long timeout_ms);
extern int kern_net_established(int fd);
extern int kern_net_close(int fd);
extern int kern_audio_ready(void);
extern long long kern_audio_play(const unsigned char *pcm, long long len,
                                 int rate, int channels, int volume);
extern void kern_audio_stop(void);
extern long long kern_audio_busy(void);
extern int kern_c_heap_stats(unsigned long long *free_total, unsigned long long *largest);

#define KERN_TIMER_SLOTS 32
static struct {
    unsigned long long deadline_ms;
    PyObject *callable;
} kern_timers[KERN_TIMER_SLOTS];

/* ---- M5: process management ----
 *
 * A "process" is a kernel thread running a Python script.  Each process
 * thread creates its own PyThreadState and takes the (single, per-3.12
 * interpreter) GIL, so only one runs Python at a time; the kernel's
 * preemptive slices plus CPython's GIL eval-breaker interleave them.
 * kill() is a graceful request: the script polls kern.killed(). */

#define MAX_PROC 16
struct kern_proc {
    int pid;    /* kernel thread slot; 0 = free */
    int state;  /* 0 = starting, 1 = running, 2 = exited */
    int killed; /* graceful termination requested */
    char name[64];
};
static struct kern_proc procs[MAX_PROC];

struct proc_arg {
    char path[128];
    int slot;
};

static void *proc_trampoline(void *arg) {
    struct proc_arg *pa = (struct proc_arg *)arg;
    int slot = pa->slot;
    free(pa);
    procs[slot].state = 1;
    procs[slot].pid = (int)pthread_self();
    /* Take the GIL and create this thread's own thread state (the standard
       pattern for C threads; with our shared tstate global the GIL is
       dropped -> current tstate is NULL, so Ensure() creates a fresh one). */
    PyGILState_STATE gs = PyGILState_Ensure();
    FILE *f = fopen(procs[slot].name, "r");
    if (f) {
        /* Run the script with a FRESH globals dict so each process has its
           own module-level namespace (PyRun_SimpleFile would execute every
           process into the interpreter's shared __main__ globals, so two
           processes would clobber each other's module variables). */
        long sz = lseek(fileno(f), 0, 2); /* SEEK_END */
        lseek(fileno(f), 0, 0);           /* rewind */
        if (sz > 0) {
            char *buf = (char *)malloc((size_t)sz + 1);
            if (buf) {
                size_t got = fread(buf, 1, (size_t)sz, f);
                buf[got] = 0;
                PyObject *g = PyDict_New();
                if (g) {
                    PyObject *co = Py_CompileString(buf, procs[slot].name,
                                                    Py_file_input);
                    if (co) {
                        PyObject *res = PyEval_EvalCode(co, g, g);
                        Py_XDECREF(res);
                        Py_DECREF(co);
                    }
                    Py_DECREF(g);
                }
                free(buf);
            }
        }
        fclose(f);
    } else {
        serial_write("[p fopen FAILED]\n", 17);
    }
    PyGILState_Release(gs);
    procs[slot].state = 2;
    return NULL;
}

static PyObject *kern_write(PyObject *self, PyObject *args) {
    const char *s;
    if (!PyArg_ParseTuple(args, "s", &s)) {
        return NULL;
    }
    serial_write(s, strlen(s));
    Py_RETURN_NONE;
}

static PyObject *kern_tick(PyObject *self, PyObject *args) {
    return PyLong_FromUnsignedLongLong(kern_tick_ms());
}

static PyObject *kern_after(PyObject *self, PyObject *args) {
    unsigned long long ms;
    PyObject *cb;
    if (!PyArg_ParseTuple(args, "KO", &ms, &cb)) {
        return NULL;
    }
    if (!PyCallable_Check(cb)) {
        PyErr_SetString(PyExc_TypeError, "kern.after: callback is not callable");
        return NULL;
    }
    for (int i = 0; i < KERN_TIMER_SLOTS; i++) {
        if (kern_timers[i].callable == NULL) {
            Py_INCREF(cb);
            kern_timers[i].callable = cb;
            kern_timers[i].deadline_ms = kern_tick_ms() + ms;
            Py_RETURN_NONE;
        }
    }
    PyErr_SetString(PyExc_RuntimeError, "kern.after: no timer slots free");
    return NULL;
}

static PyObject *kern_sleep(PyObject *self, PyObject *args) {
    unsigned long long ms;
    if (!PyArg_ParseTuple(args, "K", &ms)) {
        return NULL;
    }
    unsigned long long target = kern_tick_ms() + ms;
    /* Drop the GIL so other processes/threads can run while we wait. */
    Py_BEGIN_ALLOW_THREADS
    while (kern_tick_ms() < target) {
        sched_yield();
    }
    Py_END_ALLOW_THREADS
    Py_RETURN_NONE;
}

static PyObject *kern_spawn(PyObject *self, PyObject *args) {
    const char *path;
    if (!PyArg_ParseTuple(args, "s", &path)) {
        return NULL;
    }
    int slot = -1;
    for (int i = 0; i < MAX_PROC; i++) {
        if (procs[i].pid == 0) {
            slot = i;
            break;
        }
    }
    if (slot < 0) {
        PyErr_SetString(PyExc_RuntimeError, "no process slots free");
        return NULL;
    }
    memset(&procs[slot], 0, sizeof(procs[slot]));
    snprintf(procs[slot].name, sizeof(procs[slot].name), "%s", path);
    struct proc_arg *pa = (struct proc_arg *)malloc(sizeof(struct proc_arg));
    if (!pa) {
        return PyErr_NoMemory();
    }
    snprintf(pa->path, sizeof(pa->path), "%s", path);
    pa->slot = slot;
    pthread_t tid;
    int rc = pthread_create(&tid, NULL, proc_trampoline, pa);
    if (rc != 0) {
        free(pa);
        memset(&procs[slot], 0, sizeof(procs[slot]));
        PyErr_SetString(PyExc_RuntimeError, "pthread_create failed");
        return NULL;
    }
    procs[slot].pid = (int)tid;
    return PyLong_FromLong((long)tid);
}

static PyObject *kern_ps(PyObject *self, PyObject *args) {
    PyObject *list = PyList_New(0);
    if (!list) {
        return NULL;
    }
    for (int i = 0; i < MAX_PROC; i++) {
        if (procs[i].pid == 0) {
            continue;
        }
        PyObject *t = Py_BuildValue("(iis)", procs[i].pid, procs[i].state,
                                    procs[i].name);
        if (!t) {
            Py_DECREF(list);
            return NULL;
        }
        int ok = PyList_Append(list, t);
        Py_DECREF(t);
        if (ok < 0) {
            Py_DECREF(list);
            return NULL;
        }
    }
    return list;
}

static PyObject *kern_kill(PyObject *self, PyObject *args) {
    int pid;
    if (!PyArg_ParseTuple(args, "i", &pid)) {
        return NULL;
    }
    for (int i = 0; i < MAX_PROC; i++) {
        if (procs[i].pid == pid) {
            procs[i].killed = 1;
            Py_RETURN_NONE;
        }
    }
    PyErr_SetString(PyExc_ProcessLookupError, "no such process");
    return NULL;
}

static PyObject *kern_killed(PyObject *self, PyObject *args) {
    int me = (int)pthread_self();
    for (int i = 0; i < MAX_PROC; i++) {
        if (procs[i].pid == me) {
            return PyLong_FromLong(procs[i].killed ? 1 : 0);
        }
    }
    return PyLong_FromLong(0);
}

static PyObject *kern_pid(PyObject *self, PyObject *args) {
    return PyLong_FromLong((long)pthread_self());
}

static PyObject *kern_fb_info(PyObject *self, PyObject *args) {
    unsigned w = 0, h = 0, stride = 0, bpp = 0;
    int format = 0;
    if (!kern_fb_present()) {
        Py_RETURN_NONE;
    }
    kern_fb_get(&w, &h, &stride, &bpp, &format);
    return Py_BuildValue("(IIIIi)", w, h, stride, bpp, format);
}

static PyObject *kern_fb_mem(PyObject *self, PyObject *args) {
    void *addr = kern_fb_addr();
    unsigned long long len = kern_fb_len();
    if (!addr || len == 0) {
        Py_RETURN_NONE;
    }
    /* Zero-copy writable memoryview over the mapped framebuffer.  The
       kernel keeps the mapping alive for the lifetime of the OS, so the
       memoryview stays valid even after the backing object is dropped. */
    return PyMemoryView_FromMemory((char *)addr, (Py_ssize_t)len, PyBUF_WRITE);
}

static PyObject *kern_alloc_stats(PyObject *self, PyObject *args) {
    PyObject *used = PyLong_FromUnsignedLongLong(kern_alloc_used());
    PyObject *total = PyLong_FromUnsignedLongLong(kern_alloc_total());
    if (!used || !total) {
        Py_XDECREF(used);
        Py_XDECREF(total);
        return NULL;
    }
    PyObject *t = PyTuple_Pack(2, used, total);
    Py_DECREF(used);
    Py_DECREF(total);
    return t;
}

static PyObject *kern_key_events(PyObject *self, PyObject *args) {
    PyObject *list = PyList_New(0);
    if (!list) {
        return NULL;
    }
    struct kern_key_evt ev;
    while (kern_key_event_pop(&ev)) {
        PyObject *name = PyUnicode_FromString(ev.name);
        if (!name) {
            Py_DECREF(list);
            return NULL;
        }
        PyObject *t = Py_BuildValue("(Nii)", name, ev.ch, (int)ev.pressed);
        if (!t) {
            Py_DECREF(list);
            return NULL;
        }
        int ok = PyList_Append(list, t);
        Py_DECREF(t);
        if (ok < 0) {
            Py_DECREF(list);
            return NULL;
        }
    }
    return list;
}

static PyObject *kern_on_key(PyObject *self, PyObject *args) {
    PyObject *cb;
    if (!PyArg_ParseTuple(args, "O", &cb)) {
        return NULL;
    }
    if (!PyCallable_Check(cb)) {
        PyErr_SetString(PyExc_TypeError, "kern.on_key: callback is not callable");
        return NULL;
    }
    Py_XINCREF(cb);
    Py_XSETREF(key_callback, cb);
    Py_RETURN_NONE;
}

static PyObject *kern_mouse_events(PyObject *self, PyObject *args) {
    (void)self;
    (void)args;
    PyObject *list = PyList_New(0);
    if (!list) {
        return NULL;
    }
    struct kern_mouse_evt ev;
    while (kern_mouse_event_pop(&ev)) {
        PyObject *t = Py_BuildValue("(iiii)", (int)ev.dx, (int)ev.dy,
                                    ev.buttons, (int)ev.wheel);
        if (!t) {
            Py_DECREF(list);
            return NULL;
        }
        int ok = PyList_Append(list, t);
        Py_DECREF(t);
        if (ok < 0) {
            Py_DECREF(list);
            return NULL;
        }
    }
    return list;
}

static PyObject *kern_on_mouse(PyObject *self, PyObject *args) {
    (void)self;
    PyObject *cb;
    if (!PyArg_ParseTuple(args, "O", &cb)) {
        return NULL;
    }
    if (!PyCallable_Check(cb)) {
        PyErr_SetString(PyExc_TypeError, "kern.on_mouse: callback is not callable");
        return NULL;
    }
    Py_XINCREF(cb);
    Py_XSETREF(mouse_callback, cb);
    Py_RETURN_NONE;
}

static PyObject *shim_fb_set_mode(PyObject *self, PyObject *args) {
    (void)self;
    unsigned w, h;
    if (!PyArg_ParseTuple(args, "II", &w, &h)) {
        return NULL;
    }
    if (!kern_fb_set_mode(w, h)) {
        Py_RETURN_NONE;
    }
    return Py_BuildValue("(IIIIi)", w, h, w, 3, 0); /* w, h, stride, bpp, fmt */
}

/* ---- M8: interactive REPL ---- */

/* Persistent namespace shared by every kern.eval call (the shell's __main__). */
static PyObject *repl_globals = NULL;

/* Format the pending exception as a one-line str ("NameError: ...") and
   clear the error indicator.  Returns a new reference, or NULL on failure
   (caller should PyErr_Print). */
static PyObject *repl_exc_str(void) {
    PyObject *exc = NULL, *val = NULL, *tb = NULL;
    PyErr_Fetch(&exc, &val, &tb);
    /* void in 3.12; on failure it clears all three. */
    PyErr_NormalizeException(&exc, &val, &tb);
    PyObject *s = NULL;
    if (val != NULL) {
        s = PyObject_Str(val);
        Py_DECREF(val);
    }
    Py_XDECREF(exc);
    Py_XDECREF(tb);
    if (s == NULL) {
        PyErr_Clear();
        s = PyUnicode_FromString("<unknown error>");
    }
    return s;
}

/* Serial-echo a unicode string (one line) and return it to the caller. */
static PyObject *repl_return_str(PyObject *s) {
    const char *u8 = PyUnicode_AsUTF8(s);
    if (u8 != NULL) {
        serial_write(u8, strlen(u8));
        serial_write("\n", 1);
    }
    return s;
}

/* Evaluate one line of Python in the persistent REPL namespace.

   Interactive semantics: bare expressions echo their repr (like CPython's
   REPL); statements produce no output; exceptions come back as a one-line
   message.  Returns None for no output, else a (kind, text) tuple where
   kind is "ok" or "err".  Everything is also echoed to the serial console
   so a driver script can read the transcript. */
static PyObject *kern_eval(PyObject *self, PyObject *args) {
    const char *src;
    const char *fname = "<repl>";
    if (!PyArg_ParseTuple(args, "s|s", &src, &fname)) {
        return NULL;
    }
    if (repl_globals == NULL) {
        repl_globals = PyDict_New();
        if (repl_globals == NULL) {
            return NULL;
        }
        PyObject *builtins = PyEval_GetBuiltins();
        if (builtins != NULL &&
            PyDict_SetItemString(repl_globals, "__builtins__", builtins) < 0) {
            Py_CLEAR(repl_globals);
            return NULL;
        }
    }
    /* Interactive compilation expects a trailing newline. */
    size_t n = strlen(src);
    int need_nl = (n == 0 || src[n - 1] != '\n');
    char *buf = (char *)malloc(n + 2);
    if (buf == NULL) {
        return PyErr_NoMemory();
    }
    memcpy(buf, src, n);
    if (need_nl) {
        buf[n++] = '\n';
    }
    buf[n] = '\0';

    PyObject *code = Py_CompileString(buf, fname, Py_single_input);
    free(buf);
    if (code == NULL) {
        PyObject *s = repl_exc_str();
        if (s == NULL) {
            PyErr_Print();
            Py_RETURN_NONE;
        }
        return Py_BuildValue("(sN)", "err", repl_return_str(s));
    }
    PyObject *res = PyEval_EvalCode(code, repl_globals, repl_globals);
    Py_DECREF(code);
    if (res == NULL) {
        PyObject *s = repl_exc_str();
        if (s == NULL) {
            PyErr_Print();
            Py_RETURN_NONE;
        }
        return Py_BuildValue("(sN)", "err", repl_return_str(s));
    }
    if (res == Py_None) {
        Py_DECREF(res);
        Py_RETURN_NONE;
    }
    /* Interactive printing: bare expressions echo their repr. */
    PyObject *r = PyObject_Repr(res);
    Py_DECREF(res);
    if (r == NULL) {
        PyErr_Print();
        Py_RETURN_NONE;
    }
    PyObject *u = PyUnicode_FromObject(r);
    Py_DECREF(r);
    if (u == NULL) {
        PyErr_Print();
        Py_RETURN_NONE;
    }
    return Py_BuildValue("(sN)", "ok", repl_return_str(u));
}

static PyObject *kern_speaker(PyObject *self, PyObject *args) {
    (void)self;
    unsigned hz;
    if (!PyArg_ParseTuple(args, "I", &hz)) {
        return NULL;
    }
    kern_speaker_freq(hz);
    Py_RETURN_NONE;
}

static PyObject *kern_speaker_off_py(PyObject *self, PyObject *args) {
    (void)self;
    (void)args;
    kern_speaker_off();
    Py_RETURN_NONE;
}

static PyObject *kern_audio_ready_py(PyObject *self, PyObject *args) {
    (void)self;
    (void)args;
    return PyBool_FromLong(kern_audio_ready() != 0);
}

static PyObject *kern_c_heap_stats_py(PyObject *self, PyObject *args) {
    (void)self;
    (void)args;
    unsigned long long ft = 0, lg = 0;
    kern_c_heap_stats(&ft, &lg);
    return Py_BuildValue("(KK)", ft, lg);
}

static PyObject *kern_audio_play_py(PyObject *self, PyObject *args) {
    (void)self;
    Py_buffer view;
    int rate;
    int channels;
    int volume = 128;
    if (!PyArg_ParseTuple(args, "y*ii|i", &view, &rate, &channels, &volume)) {
        return NULL;
    }
    long long n = kern_audio_play((const unsigned char *)view.buf,
                                  (long long)view.len, rate, channels, volume);
    PyBuffer_Release(&view);
    if (n < 0) {
        Py_RETURN_NONE;
    }
    return PyLong_FromLongLong(n);
}

static PyObject *kern_audio_stop_py(PyObject *self, PyObject *args) {
    (void)self;
    (void)args;
    kern_audio_stop();
    Py_RETURN_NONE;
}

static PyObject *kern_audio_busy_py(PyObject *self, PyObject *args) {
    (void)self;
    (void)args;
    return PyLong_FromLongLong(kern_audio_busy());
}

static PyObject *kern_net_ready_py(PyObject *self, PyObject *args) {
    (void)self;
    (void)args;
    return PyBool_FromLong(kern_net_ready() != 0);
}

static PyObject *kern_net_ip_py(PyObject *self, PyObject *args) {
    (void)self;
    (void)args;
    unsigned int ip = kern_net_ip();
    return Py_BuildValue("(BBBB)", ip & 0xFF, (ip >> 8) & 0xFF,
                         (ip >> 16) & 0xFF, (ip >> 24) & 0xFF);
}

static PyObject *kern_net_mac_py(PyObject *self, PyObject *args) {
    (void)self;
    (void)args;
    unsigned char m[6];
    kern_net_mac(m);
    return Py_BuildValue("(BBBBBB)", m[0], m[1], m[2], m[3], m[4], m[5]);
}

static PyObject *kern_net_socket_py(PyObject *self, PyObject *args) {
    (void)self;
    int is_tcp;
    if (!PyArg_ParseTuple(args, "p", &is_tcp)) {
        return NULL;
    }
    return PyLong_FromLong(kern_net_socket(is_tcp ? 1 : 0));
}

static PyObject *kern_net_connect_py(PyObject *self, PyObject *args) {
    (void)self;
    int fd;
    unsigned int ip;
    unsigned short port;
    if (!PyArg_ParseTuple(args, "iIH", &fd, &ip, &port)) {
        return NULL;
    }
    return PyLong_FromLong(kern_net_connect(fd, ip, port));
}

static PyObject *kern_net_send_py(PyObject *self, PyObject *args) {
    (void)self;
    int fd;
    Py_buffer view;
    if (!PyArg_ParseTuple(args, "iy*", &fd, &view)) {
        return NULL;
    }
    long long n = kern_net_send(fd, (const unsigned char *)view.buf,
                                (long long)view.len);
    PyBuffer_Release(&view);
    return PyLong_FromLongLong(n);
}

static PyObject *kern_net_recv_py(PyObject *self, PyObject *args) {
    (void)self;
    int fd;
    int cap = 65536;
    if (!PyArg_ParseTuple(args, "i|i", &fd, &cap)) {
        return NULL;
    }
    if (cap <= 0 || cap > 4 * 1024 * 1024) {
        cap = 65536;
    }
    PyObject *ba = PyByteArray_FromStringAndSize(NULL, cap);
    if (!ba) {
        return NULL;
    }
    long long n = kern_net_recv(fd, (unsigned char *)PyByteArray_AS_STRING(ba),
                                (long long)cap);
    if (n < 0) {
        Py_DECREF(ba);
        Py_RETURN_NONE;
    }
    if (n == 0) {
        Py_DECREF(ba);
        return PyBytes_FromStringAndSize("", 0);
    }
    PyObject *b = PyBytes_FromStringAndSize(PyByteArray_AS_STRING(ba), (Py_ssize_t)n);
    Py_DECREF(ba);
    return b;
}

static PyObject *kern_net_poll_py(PyObject *self, PyObject *args) {
    (void)self;
    int fd;
    long long timeout_ms = 0;
    if (!PyArg_ParseTuple(args, "i|L", &fd, &timeout_ms)) {
        return NULL;
    }
    return PyLong_FromLong(kern_net_poll(fd, timeout_ms));
}

static PyObject *kern_net_established_py(PyObject *self, PyObject *args) {
    (void)self;
    int fd;
    if (!PyArg_ParseTuple(args, "i", &fd)) {
        return NULL;
    }
    return PyLong_FromLong(kern_net_established(fd));
}

static PyObject *kern_net_close_py(PyObject *self, PyObject *args) {
    (void)self;
    int fd;
    if (!PyArg_ParseTuple(args, "i", &fd)) {
        return NULL;
    }
    return PyLong_FromLong(kern_net_close(fd));
}

static PyObject *kern_poweroff(PyObject *self, PyObject *args) {
    (void)self;
    (void)args;
    /* QEMU's isa-debug-exit device: any write to iobase 0xf4 ends the VM. */
    __asm__ volatile("outl %0, %1" : : "a"(0x11u), "d"((unsigned short)0xf4));
    for (;;) {
        __asm__ volatile("hlt");
    }
    return NULL;
}

static PyMethodDef kern_methods[] = {
    {"write", kern_write, METH_VARARGS, "write a string to the serial console"},
    {"eval", kern_eval, METH_VARARGS, "evaluate a line of Python in the persistent REPL namespace; returns (kind, text) or None"},
    {"poweroff", kern_poweroff, METH_NOARGS, "power the machine off (QEMU isa-debug-exit)"},
    {"tick", kern_tick, METH_NOARGS, "monotonic time in milliseconds"},
    {"after", kern_after, METH_VARARGS, "schedule a callback after ms"},
    {"sleep", kern_sleep, METH_VARARGS, "sleep for ms (releases the GIL)"},
    {"alloc_stats", kern_alloc_stats, METH_NOARGS, "kernel heap (used, total)"},
    {"fb_info", kern_fb_info, METH_NOARGS, "framebuffer geometry (w, h, stride, bpp, format) or None"},
    {"fb_mem", kern_fb_mem, METH_NOARGS, "zero-copy writable memoryview of the framebuffer or None"},
    {"key_events", kern_key_events, METH_NOARGS, "pending keyboard events as (name, ch, pressed) tuples"},
    {"on_key", kern_on_key, METH_VARARGS, "register a callback for key events"},
    {"mouse_events", kern_mouse_events, METH_NOARGS, "pending mouse packets as (dx, dy, buttons) tuples"},
    {"on_mouse", kern_on_mouse, METH_VARARGS, "register a callback for mouse packets"},
    {"fb_set_mode", shim_fb_set_mode, METH_VARARGS, "change the display resolution via Bochs VBE (QEMU stdvga); returns new (w,h,stride,bpp,fmt) or None"},
    {"spawn", kern_spawn, METH_VARARGS, "start a process running a script; returns pid"},
    {"ps", kern_ps, METH_NOARGS, "list processes as (pid, state, name) tuples"},
    {"kill", kern_kill, METH_VARARGS, "request graceful termination of a process"},
    {"killed", kern_killed, METH_NOARGS, "True if the calling process was asked to terminate"},
    {"pid", kern_pid, METH_NOARGS, "kernel thread id of the calling process"},
    {"speaker", kern_speaker, METH_VARARGS, "set the PC speaker frequency (0 = off)"},
    {"speaker_off", kern_speaker_off_py, METH_NOARGS, "silence the PC speaker"},
    {"c_heap_stats", kern_c_heap_stats_py, METH_NOARGS, "C-heap free list: (total free bytes, largest free block)"},
{"audio_ready", kern_audio_ready_py, METH_NOARGS, "True when the AC97 audio device is present"},
    {"audio_play", kern_audio_play_py, METH_VARARGS, "play 16-bit LE PCM (bytes, rate, channels[, volume 0..128]); returns bytes buffered or None"},
    {"audio_stop", kern_audio_stop_py, METH_NOARGS, "stop audio and drain the DMA ring"},
    {"audio_busy", kern_audio_busy_py, METH_NOARGS, "bytes currently buffered in the audio ring"},
    {"net_ready", kern_net_ready_py, METH_NOARGS, "True when the network stack is up (e1000 + smoltcp)"},
    {"net_ip", kern_net_ip_py, METH_NOARGS, "local IPv4 as (a, b, c, d)"},
    {"net_mac", kern_net_mac_py, METH_NOARGS, "MAC address as (6) tuple"},
    {"net_socket", kern_net_socket_py, METH_VARARGS, "open a socket (tcp=True/False); returns fd or None"},
    {"net_connect", kern_net_connect_py, METH_VARARGS, "TCP connect (fd, ip-u32, port); non-blocking"},
    {"net_send", kern_net_send_py, METH_VARARGS, "send bytes (fd, data); returns n or None if would-block"},
    {"net_recv", kern_net_recv_py, METH_VARARGS, "recv bytes (fd[, cap]); None=would-block, b''=closed"},
    {"net_poll", kern_net_poll_py, METH_VARARGS, "wait for socket readiness (fd, timeout_ms); 1=ready 0=timeout"},
    {"net_established", kern_net_established_py, METH_VARARGS, "1 when the TCP connection is established (fd)"},
    {"net_close", kern_net_close_py, METH_VARARGS, "close a socket (fd)"},
    {NULL, NULL, 0, NULL}
};

static struct PyModuleDef kern_module = {
    PyModuleDef_HEAD_INIT,
    "kern",
    "kernel services: console, timers, memory",
    -1,
    kern_methods
};

PyMODINIT_FUNC PyInit_kern(void) {
    return PyModule_Create(&kern_module);
}

/* Boot-time import/FS capability self-check: frozen/builtin and
   initramfs-backed stdlib must import; exercises open/read/stat and the
   /dev pseudo-devices. */
void kern_import_selftest(void) {
    static const char script[] =
        "import sys\n"
        "import kern\n"
        "import os\n"
        "def try_import(name):\n"
        "    try:\n"
        "        __import__(name)\n"
        "        kern.write('import %-26s OK\\n' % name)\n"
        "    except Exception as e:\n"
        "        kern.write('import %-26s FAIL: %s\\n' % (name, type(e).__name__))\n"
        "kern.write('--- import self-check (%d builtin modules) ---\\n' % len(sys.builtin_module_names))\n"
        "for n in ['json','re','collections','functools','threading','copyreg','importlib.util','html.parser','xml.etree.ElementTree','logging']:\n"
        "    try_import(n)\n"
        "# Third-party libraries (build-time embedding, no pip):\n"
        "try:\n"
        "    from termcolor import colored\n"
        "    s = colored('third-party', 'green')\n"
        "    kern.write('import %-26s OK (3rd-party pure-Python: %r)\\n' % ('termcolor', s))\n"
        "except Exception as e:\n"
        "    kern.write('import termcolor FAIL: %s\\n' % (e,))\n"
        "try:\n"
        "    import six\n"
        "    kern.write('import %-26s OK (3rd-party, PY3=%s)\\n' % ('six', six.PY3))\n"
        "except Exception as e:\n"
        "    kern.write('import six FAIL: %s\\n' % (e,))\n"
        "# C modules built into the kernel + the pure-Python stdlib they unlock:\n"
        "for n in ['struct','array','math','binascii','datetime','csv','random','bisect','heapq','queue','statistics','base64','decimal','string']:\n"
        "    try_import(n)\n"
        "try:\n"
        "    import struct\n"
        "    kern.write('struct pack/unpack: %r\\n' % (struct.unpack('>HHI', struct.pack('>HHI', 0x1234, 0x5678, 0x9abcdef0)),))\n"
        "except Exception as e:\n"
        "    kern.write('struct func FAIL: %s\\n' % (e,))\n"
        "try:\n"
        "    import math\n"
        "    kern.write('math repr: sqrt(2)=%r sin(1)=%r lgamma(5)=%r erf(1)=%r\\n' % (math.sqrt(2), math.sin(1), math.lgamma(5), math.erf(1)))\n"
        "    kern.write('math fmt:  sqrt(2)=%.4f sin(1)=%.4f lgamma(5)=%.2f erf(1)=%.4f\\n' % (math.sqrt(2), math.sin(1), math.lgamma(5), math.erf(1)))\n"
        "    kern.write('shim fmt: 1.4142=%.4f 0.8415=%.4f 3.1781=%.2f 0.8427=%.4f\\n' % (1.4142135623730951, 0.8414709848078965, 3.1780538303479458, 0.8427007929497149))\n"
        "except Exception as e:\n"
        "    kern.write('math func FAIL: %s\\n' % (e,))\n"
        "try:\n"
        "    import binascii\n"
        "    kern.write('binascii: %r\\n' % (binascii.hexlify(b'Ripos'),))\n"
        "except Exception as e:\n"
        "    kern.write('binascii func FAIL: %s\\n' % (e,))\n"
        "try:\n"
        "    import datetime\n"
        "    kern.write('datetime: %s\\n' % (datetime.datetime(2026, 8, 16, 12, 0, 0).isoformat(),))\n"
        "except Exception as e:\n"
        "    kern.write('datetime func FAIL: %s\\n' % (e,))\n"
        "try:\n"
        "    import array\n"
        "    a = array.array('i', [1, 2, 3])\n"
        "    kern.write('array: %r sum=%d\\n' % (a.tolist(), sum(a)))\n"
        "except Exception as e:\n"
        "    kern.write('array func FAIL: %s\\n' % (e,))\n"
        "try:\n"
        "    import hashlib\n"
        "    kern.write('hashlib: sha256=%s md5=%s\\n' % (hashlib.sha256(b'Ripos').hexdigest()[:16], hashlib.md5(b'Ripos').hexdigest()[:16]))\n"
        "except Exception as e:\n"
        "    kern.write('hashlib FAIL: %r\\n' % (e,))\n"
        "for n in ['decimal', '_pydecimal', 'statistics']:\n"
        "    try:\n"
        "        __import__(n)\n"
        "        kern.write('import %-26s OK\\n' % n)\n"
        "    except Exception as e:\n"
        "        kern.write('import %-26s FAIL: %r\\n' % (n, e))\n"
        "try:\n"
        "    import decimal\n"
        "    kern.write('decimal: %s\\n' % (decimal.Decimal('3.14159') * 2,))\n"
        "except Exception as e:\n"
        "    kern.write('decimal func FAIL: %r\\n' % (e,))\n"
        "try:\n"
        "    motd = open('/etc/motd').read()\n"
        "    kern.write('motd: %r\\n' % (motd.splitlines()[0],))\n"
        "except Exception as e:\n"
        "    kern.write('motd FAIL: %s\\n' % (e,))\n"
        "try:\n"
        "    import json\n"
        "    data = json.dumps({'hello': 42, 'items': [1, 2, 3], 'ratio': 0.5})\n"
        "    back = json.loads(data)\n"
        "    kern.write('json roundtrip: %s -> %r\\n' % (data, back))\n"
        "except Exception as e:\n"
        "    kern.write('json FAIL: %s\\n' % (e,))\n"
        "try:\n"
        "    d = open('/dev/serial', 'w')\n"
        "    d.write('hello from /dev/serial\\n')\n"
        "    d.close()\n"
        "except Exception as e:\n"
        "    kern.write('/dev/serial FAIL: %s\\n' % (e,))\n"
        "try:\n"
        "    kern.write('os.listdir(/): %s\\n' % (os.listdir('/')[:6],))\n"
        "except Exception as e:\n"
        "    kern.write('listdir FAIL: %s\\n' % (e,))\n"
        "kern.write('fs self-check done\\n')\n";
    (void)PyRun_SimpleString(script);
}

/* Fire due Python timer callbacks and deliver pending key events.  Called
   from the kernel main loop with the GIL held by the main thread. */
void kern_process_events(void) {
    unsigned long long now = kern_tick_ms();
    for (int i = 0; i < KERN_TIMER_SLOTS; i++) {
        if (kern_timers[i].callable != NULL && kern_timers[i].deadline_ms <= now) {
            PyObject *cb = kern_timers[i].callable;
            kern_timers[i].callable = NULL;
            PyObject *res = PyObject_CallObject(cb, NULL);
            Py_DECREF(cb);
            if (res) {
                Py_DECREF(res);
            }
            else {
                PyErr_Print(); /* traceback to the serial console */
            }
        }
    }
    if (key_callback != NULL) {
        struct kern_key_evt ev;
        while (kern_key_event_pop(&ev)) {
            PyObject *name = PyUnicode_FromString(ev.name);
            if (!name) {
                PyErr_Print();
                continue;
            }
            PyObject *arg = Py_BuildValue("(Nii)", name, ev.ch, (int)ev.pressed);
            if (!arg) {
                PyErr_Print();
                continue;
            }
            PyObject *res = PyObject_CallOneArg(key_callback, arg);
            Py_DECREF(arg);
            if (res) {
                Py_DECREF(res);
            }
            else {
                PyErr_Print();
            }
        }
    }
    if (mouse_callback != NULL) {
        struct kern_mouse_evt ev;
        while (kern_mouse_event_pop(&ev)) {
            PyObject *arg = Py_BuildValue("(iiii)", (int)ev.dx, (int)ev.dy,
                                          ev.buttons, (int)ev.wheel);
            if (!arg) {
                PyErr_Print();
                continue;
            }
            PyObject *res = PyObject_CallOneArg(mouse_callback, arg);
            Py_DECREF(arg);
            if (res) {
                Py_DECREF(res);
            }
            else {
                PyErr_Print();
            }
        }
    }
}
