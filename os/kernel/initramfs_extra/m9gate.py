# m9gate.py -- serial-visible mouse/log helper for the M9 QEMU gate.
# `import m9gate` from the shell registers a mouse handler that echoes every
# event to the serial console, so the harness can prove PS/2 mouse packets
# flow kernel -> Python -> drivers end to end.
import kern
import mouse

def _h(ev):
    kern.write('m9 mouse=%s\n' % (ev,))

mouse.DEFAULT.on_event(_h)
kern.write('m9gate: mouse handler registered\n')
