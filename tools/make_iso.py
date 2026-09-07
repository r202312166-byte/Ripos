#!/usr/bin/env python3
"""make_iso.py -- wrap a bootable disk image (bios.img) in an El Torito ISO.

Ripos boots via the bootloader-crate BIOS chain from a raw MBR disk image;
the bootloader crate has no ISO output, so this tool builds a minimal
ISO-9660 volume with an El Torito boot catalog (hard-disk emulation) that
points at the disk image.  Booting it presents the image to the BIOS as a
hard disk, so the whole stage-1..kernel chain works as from a real disk.

Layout (2048-byte sectors):
   16  PVD
   17  El Torito boot record
   18  volume descriptor set terminator
   19  boot catalog
   20.. boot image (bios.img), padded to a whole 512-byte sector

Usage:  python make_iso.py <bios.img> <out.iso>
"""
import struct, sys

SECTOR = 2048


def pad(b, size):
    return b + bytes((-len(b)) % size)


def both_endian_u32(v):
    return struct.pack("<I", v) + struct.pack(">I", v)


def pvd(vol_id=b"RIPOS", root_record=b""):
    d = bytearray(2048)
    d[0] = 1                      # type: primary volume descriptor
    d[1:6] = b"CD001"
    d[6] = 1                      # version
    d[40:72] = vol_id.ljust(32)[:32]
    d[128:132] = struct.pack("<H", 2048) + struct.pack(">H", 2048)
    d[156:190] = root_record     # root directory record
    d[190:318] = b"RIPOS ISO".ljust(128)[:128]
    d[881] = 1                    # file structure version
    return d


def eltorito_boot_record(catalog_lba):
    d = bytearray(2048)
    d[0] = 0                      # type: boot record
    d[1:6] = b"CD001"
    d[6] = 1
    # "EL TORITO SPECIFICATION" occupies bytes 7..38 (SeaBIOS compares
    # buffer[1..] against "CD001\x01" + this string with strcmp, so the
    # trailing padding must be NUL, not spaces).
    d[7:39] = b"EL TORITO SPECIFICATION" + b"\x00" * 9   # 23 + 9 = 32
    # Boot Catalog Pointer: SeaBIOS reads a little-endian u32 at offset 0x47
    # (byte 71).  Assign exactly 4 bytes -- a longer slice would grow the
    # bytearray and shift every later sector.
    d[71:75] = struct.pack("<I", catalog_lba)
    return d


def terminator():
    d = bytearray(2048)
    d[0] = 255
    d[1:6] = b"CD001"
    d[6] = 1
    return d


def boot_catalog(boot_lba, boot_sectors_512, media_type=0x04):
    """El Torito boot catalog: validation + initial/default entry.
    media_type 0x04 = hard-disk emulation (boot the raw image as a disk).
    The 16-bit sector-count field limits spec-compliant HD emulation to
    32 MiB; larger images are written anyway (the field carries the low
    16 bits) and some emulators derive the size from the ISO extent."""
    d = bytearray(2048)
    # validation entry
    d[0] = 0x01
    d[1] = 0x00                    # platform: x86
    d[4:28] = b"RIPOS".ljust(24)[:24]
    d[30:32] = b"\x55\xaa"
    # checksum: sum of all 16-bit words == 0 (the 0x55AA word included)
    s = sum(struct.unpack("<8H", d[0:16]))
    d[28:30] = struct.pack("<H", (-s) & 0xFFFF)
    # initial/default entry
    d[32] = 0x88                   # bootable
    d[33] = media_type
    d[34:36] = b"\x00\x00"        # load segment 0x0000 (HD emul)
    d[36] = 0                      # system type
    d[38:40] = struct.pack("<H", boot_sectors_512 & 0xFFFF)
    d[40:44] = struct.pack("<I", boot_lba)
    # termination entry
    d[48] = 0x00
    return d


def root_dir_record(lba, size):
    """Minimal root directory record (no file entries)."""
    r = bytearray()
    r += bytes([34])               # record length
    r += bytes([0])                # extended attribute record length
    r += struct.pack("<I", lba) + struct.pack(">I", lba)
    r += struct.pack("<I", size) + struct.pack(">I", size)
    r += bytes([0x25, 0x25, 0x25, 0x25, 0x25, 0x25, 0x25])  # date
    r += bytes([0x02])             # flags: directory
    r += bytes([0])                # file unit size
    r += bytes([0])                # interleave gap
    r += struct.pack("<H", 1) + struct.pack(">H", 1)  # volume sequence
    r += bytes([1])                # file identifier length
    r += b"\x00"                  # "."
    # exactly 34 bytes -> the PVD slice [156:190] is 34 slots; a shorter
    # record would shrink the PVD bytearray and shift every later sector.
    return bytes(r)


def main():
    if len(sys.argv) < 3:
        print(__doc__)
        return 2
    img_path, iso_path = sys.argv[1], sys.argv[2]
    img = open(img_path, "rb").read()
    # boot image must be a whole number of 512-byte sectors
    img = pad(img, 512)
    img_sectors = len(img) // 512

    # layout
    catalog_lba = 19
    boot_lba = 20
    img_sectors_2048 = (len(img) + SECTOR - 1) // SECTOR

    vol_size = boot_lba + img_sectors_2048
    root_lba = vol_size
    vol_size += 1                   # root directory

    d_pvd = pvd(root_record=root_dir_record(root_lba, SECTOR))
    d_pvd[80:88] = both_endian_u32(vol_size)

    d_catalog = boot_catalog(boot_lba, img_sectors, media_type=0x04)

    out = bytearray()
    out += bytes(SECTOR * 16)               # system area
    out += bytes(d_pvd)                     # 16 PVD
    out += bytes(eltorito_boot_record(catalog_lba))  # 17
    out += bytes(terminator())              # 18
    out += bytes(d_catalog)                 # 19 catalog
    out += pad(img, SECTOR)                 # 20.. boot image
    out += bytes(root_dir_record(root_lba, SECTOR))  # root directory
    out += bytes(SECTOR - 34)               # pad the root dir sector

    with open(iso_path, "wb") as f:
        f.write(bytes(out))
    print("wrote %s: %d bytes, boot image %d sectors (%.1f MiB) at LBA %d"
          % (iso_path, len(out), img_sectors, len(img) / 1048576.0, boot_lba))
    return 0


if __name__ == "__main__":
    sys.exit(main())
