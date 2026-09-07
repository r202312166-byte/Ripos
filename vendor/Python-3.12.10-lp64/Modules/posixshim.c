/* posixshim.c -- host-side implementations of POSIX bits that
   mingw-w64's CRT omits, used only to complete the CPython host build
   (_freeze_module / _bootstrap_python / python links).  The kernel
   build links its own Rust shim instead of this file. */

#include <sys/stat.h>
#include <direct.h>
#include <errno.h>
#include <stdlib.h>
#include <string.h>

/* mingw has no symlink support on normal filesystems; treat lstat as
   stat. */
int
lstat(const char *path, struct stat *buf)
{
    return stat(path, buf);
}

/* POSIX mkdir(path, mode) mapped onto mingw's 1-arg _mkdir. */
int
_py_mkdir2(const char *path, mode_t mode)
{
    (void)mode;
    return _mkdir(path);
}

/* fcntl() -- the host build only needs the F_GETFD/F_GETFL/F_SETFD/
   F_SETFL surface for descriptor flag bookkeeping; the fd values are
   always valid here and the flags are not meaningful on Windows. */
int
fcntl(int fd, int cmd, ...)
{
    (void)fd;
    switch (cmd) {
    case 1: /* F_GETFD */
    case 3: /* F_GETFL */
        return 0;
    case 2: /* F_SETFD */
    case 4: /* F_SETFL */
        return 0;
    default:
        errno = EINVAL;
        return -1;
    }
}

/* nl_langinfo() -- the system is UTF-8 only. */
char *
nl_langinfo(int item)
{
    if (item == 0 /* CODESET */) {
        return (char *)"UTF-8";
    }
    return (char *)"";
}

/* setenv()/unsetenv() via the CRT environment functions. */
int
setenv(const char *name, const char *value, int overwrite)
{
    if (!overwrite && getenv(name) != NULL) {
        return 0;
    }
    if (getenv(name) != NULL || _putenv_s(name, value) == 0) {
        return 0;
    }
    errno = EINVAL;
    return -1;
}

int
unsetenv(const char *name)
{
    size_t len = strlen(name);
    char *eq = malloc(len + 2);
    if (eq == NULL) {
        errno = ENOMEM;
        return -1;
    }
    memcpy(eq, name, len);
    eq[len] = '=';
    eq[len + 1] = '\0';
    int r = _putenv(eq);
    free(eq);
    if (r != 0) {
        errno = EINVAL;
        return -1;
    }
    return 0;
}

/* pty support -- not used by the host build tooling. */
int
grantpt(int fd)
{
    (void)fd;
    errno = ENOTTY;
    return -1;
}

int
unlockpt(int fd)
{
    (void)fd;
    errno = ENOTTY;
    return -1;
}

char *
ptsname(int fd)
{
    (void)fd;
    errno = ENOTTY;
    return NULL;
}

int
ioctl(int fd, unsigned long request, ...)
{
    (void)fd;
    (void)request;
    errno = EINVAL;
    return -1;
}
