# proc2.py -- M5 demo process 2: busy-loop printer.
import kern

NAME = 'P2'
i = 0
while not kern.killed():
    if i % 5 == 0:
        kern.sleep(2)  # releases the GIL so the other process can run
    kern.write('%s:%d\n' % (NAME, i))
    i += 1
    for _ in range(3000):
        pass
kern.write('%s exiting\n' % NAME)
