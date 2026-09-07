#ifndef Py_POSIXCOMPAT_H
#define Py_POSIXCOMPAT_H

/* posixcompat.h -- supplies POSIX declarations/constants that the
   mingw-w64 CRT headers omit, so CPython can be compiled in "Unix-like"
   mode on a mingw host.  The corresponding functions are provided at
   link time by the kernel's POSIX shim (during the host build they may
   additionally resolve against mingw's libc where present). */

/* ---- fcntl() and descriptor flags ---- */
#ifndef _Py_DECL_FCNTL
#define _Py_DECL_FCNTL
int fcntl(int fd, int cmd, ...);
#endif

#ifndef F_DUPFD
#define F_DUPFD 0
#define F_GETFD 1
#define F_SETFD 2
#define F_GETFL 3
#define F_SETFL 4
#endif
#ifndef FD_CLOEXEC
#define FD_CLOEXEC 1
#endif

#ifndef O_NONBLOCK
#define O_NONBLOCK 0x800
#endif
#ifndef O_NOFOLLOW
#define O_NOFOLLOW 0x20000
#endif
#ifndef O_CLOEXEC
#define O_CLOEXEC 0x80000
#endif
#ifndef O_DIRECTORY
#define O_DIRECTORY 0x10000
#endif
#ifndef O_NOCTTY
#define O_NOCTTY 0x100
#endif
#ifndef O_DIRECT
#define O_DIRECT 0x4000
#endif
#ifndef O_SYNC
#define O_SYNC 0x101000
#endif
#ifndef O_DSYNC
#define O_DSYNC 0x1000
#endif
#ifndef O_RSYNC
#define O_RSYNC 0x101000
#endif

/* ---- nl_langinfo() ---- */
#ifndef _Py_DECL_NL_LANGINFO
#define _Py_DECL_NL_LANGINFO
char *nl_langinfo(int item);
#endif
#ifndef CODESET
#define CODESET 0
#endif

#include <sys/stat.h>

/* ---- mkdir() with POSIX mode argument ----
   mingw's io.h/direct.h declare the 1-arg form, so the 2-arg POSIX
   call is exposed under a private name (see posixmodule.c). */
#ifndef _Py_DECL_MKDIR
#define _Py_DECL_MKDIR
int _py_mkdir2(const char *_Path, mode_t _Mode);
#endif

/* ---- lstat() ---- */
#ifndef _Py_DECL_LSTAT
#define _Py_DECL_LSTAT
int lstat(const char *_Path, struct stat *_Buf);
#endif

/* ---- getlogin() / alarm() ---- */
#ifndef _Py_DECL_GETLOGIN
#define _Py_DECL_GETLOGIN
char *getlogin(void);
#endif
#ifndef _Py_DECL_ALARM
#define _Py_DECL_ALARM
unsigned int alarm(unsigned int seconds);
#endif

/* ---- setenv() / unsetenv() ---- */
#ifndef _Py_DECL_SETENV
#define _Py_DECL_SETENV
int setenv(const char *name, const char *value, int overwrite);
int unsetenv(const char *name);
#endif

/* ---- pty support and ioctl() ---- */
#ifndef _Py_DECL_IOCTL
#define _Py_DECL_IOCTL
int ioctl(int fd, unsigned long request, ...);
#endif
#ifndef _Py_DECL_PTY
#define _Py_DECL_PTY
int grantpt(int fd);
int unlockpt(int fd);
char *ptsname(int fd);
#endif
#ifndef I_PUSH
#define I_PUSH 6
#endif

/* ---- missing POSIX signal macros ---- */
#ifndef SIGHUP
#define SIGHUP 1
#endif
#ifndef SIGQUIT
#define SIGQUIT 3
#endif
#ifndef SIGPIPE
#define SIGPIPE 13
#endif
#ifndef SIGALRM
#define SIGALRM 14
#endif
#ifndef SIGCHLD
#define SIGCHLD 17
#endif
#ifndef SIGCONT
#define SIGCONT 18
#endif
#ifndef SIGSTOP
#define SIGSTOP 19
#endif
#ifndef SIGTSTP
#define SIGTSTP 20
#endif
#ifndef SIGTTIN
#define SIGTTIN 21
#endif
#ifndef SIGTTOU
#define SIGTTOU 22
#endif
#ifndef SIGBUS
#define SIGBUS 7
#endif
#ifndef SIGPROF
#define SIGPROF 27
#endif
#ifndef SIGURG
#define SIGURG 23
#endif
#ifndef SIGUSR1
#define SIGUSR1 10
#endif
#ifndef SIGUSR2
#define SIGUSR2 12
#endif
#ifndef NSIG
#define NSIG 32
#endif

#endif /* Py_POSIXCOMPAT_H */