"""osproc -- process management for Ripos (the Rust interpreted Python OS).

OS services written in high-level Python on top of the kernel's
spawn/ps/kill primitives.  This is the whole point of the project: the only
binary component is the interpreter; the OS lives in Python."""

import kern


def spawn(path):
    """Start a new process running the script at `path`; return its pid."""
    if not isinstance(path, str):
        raise TypeError('path must be str')
    import os
    if not os.path.exists(path):
        raise FileNotFoundError(path)
    return kern.spawn(path)


def kill(pid):
    """Ask process `pid` to terminate (it cooperates via kern.killed())."""
    if not isinstance(pid, int):
        raise TypeError('pid must be int')
    return kern.kill(pid)


def ps():
    """Return [(pid, state, name), ...] for every process."""
    states = {0: 'starting', 1: 'running', 2: 'exited'}
    return [(pid, states.get(st, str(st)), name)
            for (pid, st, name) in kern.ps()]


def killed():
    """True if the calling process was asked to terminate."""
    return kern.killed()
