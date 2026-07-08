"""evt.py — lift Java class files out of Episode I ``.evt`` event containers.

Episode I's cutscene scripting is literal Java: the engine embeds a JVM
class-file interpreter (402 ``Java_xeno_*`` JNI natives in SLUS_204.69), and
every ``.evt`` on the disc is an ``FL00`` container of real ``0xCAFEBABE``
class files — format 45.3 (JDK 1.1), compiled with a stock Sun javac, debug
attributes intact. ``system.evt`` even ships the runtime library
(``java/lang/Object``, ``String``, ``StringBuffer``).

Rather than trusting the FL00 file table (it mixes real classes with 24-byte
stub records, and holds extra classes in regions the table doesn't describe),
this module scans for class-file magics and walks the actual class-file
structure — constant pool, fields, methods, attributes — to recover each
class's exact length and its real fully-qualified name from the constant
pool. A blob that doesn't parse as a complete class is a stub and is skipped
(stubs carry no code; they mark classes the VM resolves from another file).

Quirk: the packer NUL-terminates constant-pool name strings so the console VM
can read them as C strings; names are stripped accordingly.
"""
from __future__ import annotations

import re
import struct
from typing import NamedTuple, Optional

CLASS_MAGIC = b"\xca\xfe\xba\xbe"
FL00_MAGIC = b"FL00"

# Java internal names: package/Class$Inner, dots never appear here.
_NAME_OK = re.compile(r"^[A-Za-z0-9_$/]+$")


class CarvedClass(NamedTuple):
    name: str      # fully-qualified internal name, e.g. "xeno/plan/Base"
    offset: int    # byte offset of the class file inside the container
    size: int      # exact class-file length


def class_at(data: bytes, off: int) -> Optional[CarvedClass]:
    """Parse a class file starting at ``off``; None if it isn't a whole one."""
    try:
        if data[off : off + 4] != CLASS_MAGIC:
            return None
        p = off + 8  # skip magic + minor/major version
        (cp_count,) = struct.unpack_from(">H", data, p)
        p += 2
        cp: dict[int, object] = {}
        i = 1
        while i < cp_count:
            tag = data[p]
            p += 1
            if tag == 1:  # Utf8
                (n,) = struct.unpack_from(">H", data, p)
                p += 2
                cp[i] = data[p : p + n]
                p += n
            elif tag == 7:  # Class -> Utf8 index
                (cp[i],) = struct.unpack_from(">H", data, p)
                p += 2
            elif tag in (3, 4, 9, 10, 11, 12):  # int/float/refs/NameAndType
                p += 4
            elif tag == 8:  # String
                p += 2
            elif tag in (5, 6):  # long/double take two constant-pool slots
                p += 8
                i += 1
            else:  # tag illegal in a 45.3 class — not a real class file
                return None
            i += 1
        p += 2  # access_flags
        (this_class,) = struct.unpack_from(">H", data, p)
        p += 4  # this_class + super_class
        (n_ifaces,) = struct.unpack_from(">H", data, p)
        p += 2 + 2 * n_ifaces

        def skip_attrs() -> None:
            nonlocal p
            (n,) = struct.unpack_from(">H", data, p)
            p += 2
            for _ in range(n):
                (alen,) = struct.unpack_from(">I", data, p + 2)
                p += 6 + alen

        for _ in range(2):  # fields, then methods
            (n,) = struct.unpack_from(">H", data, p)
            p += 2
            for _ in range(n):
                p += 6  # access, name, descriptor
                skip_attrs()
        skip_attrs()  # class-level attributes
        if p > len(data):
            return None

        utf8_idx = cp.get(this_class)
        raw = cp.get(utf8_idx) if isinstance(utf8_idx, int) else None
        name = ""
        if isinstance(raw, bytes):
            name = raw.rstrip(b"\x00").decode("utf-8", "replace")
        if not _NAME_OK.match(name):
            name = f"class_{off:06x}"
        return CarvedClass(name, off, p - off)
    except (struct.error, IndexError):
        return None


def carve_classes(data: bytes) -> tuple[list[CarvedClass], int]:
    """All whole class files in ``data`` plus the count of stub records."""
    found: list[CarvedClass] = []
    stubs = 0
    i = 0
    while True:
        i = data.find(CLASS_MAGIC, i)
        if i < 0:
            return found, stubs
        c = class_at(data, i)
        # 24-byte cafebabe records in the FL00 table are stubs, not classes.
        if c is not None and c.size > 24:
            found.append(c)
            i += c.size
        else:
            stubs += 1
            i += 4
