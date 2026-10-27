"""Big-endian binary reading and writing, shared by every format parser.

GameCube data is big-endian. Parsers read with `Reader` and write back with `Writer`, so that a
round trip (parse, write, compare) uses the same field definitions in both directions. Both take
`byteorder="<"` for the few formats whose spec allows little-endian data.

Every read is bounds-checked: reading past the end raises `ReadError` with the offset, rather
than returning short data.
"""

from __future__ import annotations

import struct

FORMATS = {
    "u8": "B",
    "u16": "H",
    "u32": "I",
    "u64": "Q",
    "s8": "b",
    "s16": "h",
    "s32": "i",
    "s64": "q",
    "f32": "f",
    "f64": "d",
}
BYTE_ORDERS = (">", "<")
_STRUCTS = {
    order: {name: struct.Struct(order + code) for name, code in FORMATS.items()}
    for order in BYTE_ORDERS
}


class ReadError(ValueError):
    """The data ended early, or an offset is out of range."""


def _structs(byteorder: str) -> dict[str, struct.Struct]:
    if byteorder not in _STRUCTS:
        raise ValueError(f"byte order must be '>' or '<', not {byteorder!r}")
    return _STRUCTS[byteorder]


def _check_alignment(alignment: int) -> None:
    if alignment < 1:
        raise ValueError(f"alignment must be at least 1, not {alignment}")


class Reader:
    """Reads big-endian values (or little-endian, with `byteorder="<"`) from `data`."""

    def __init__(self, data: bytes, offset: int = 0, byteorder: str = ">") -> None:
        self.byteorder = byteorder
        self._structs = _structs(byteorder)
        self.data = bytes(data)
        self.pos = 0
        self.seek(offset)

    def __len__(self) -> int:
        return len(self.data)

    @property
    def remaining(self) -> int:
        return len(self.data) - self.pos

    def seek(self, offset: int) -> None:
        if not 0 <= offset <= len(self.data):
            raise ReadError(f"offset {offset:#x} is outside 0 to {len(self.data):#x}")
        self.pos = offset

    def skip(self, size: int) -> None:
        self.seek(self.pos + size)

    def align(self, alignment: int) -> None:
        """Move forward to the next multiple of `alignment`."""
        _check_alignment(alignment)
        self.seek(-(-self.pos // alignment) * alignment)

    def bytes(self, size: int) -> bytes:
        if size < 0 or size > self.remaining:
            raise ReadError(f"{size} bytes at {self.pos:#x}, but only {self.remaining} remain")
        start = self.pos
        self.pos += size
        return self.data[start : self.pos]

    def cstring(self, size: int | None = None) -> bytes:
        """A NUL-terminated string, without the NUL.

        With `size`, read a fixed-size field of that many bytes and cut it at the first NUL.
        Without it, read up to and including the next NUL.
        """
        if size is not None:
            return self.bytes(size).split(b"\0", 1)[0]
        end = self.data.find(b"\0", self.pos)
        if end < 0:
            raise ReadError(f"no NUL after {self.pos:#x}")
        text = self.bytes(end - self.pos)
        self.skip(1)
        return text

    def sub(self, offset: int, size: int) -> Reader:
        """A reader over `size` bytes at `offset`, positioned at its own start."""
        if offset < 0 or size < 0 or offset + size > len(self.data):
            raise ReadError(f"{size} bytes at {offset:#x} run past the end at {len(self.data):#x}")
        return Reader(self.data[offset : offset + size], byteorder=self.byteorder)

    def read(self, kind: str) -> int | float:
        packer = self._structs[kind]
        if packer.size > self.remaining:
            raise ReadError(f"{kind} at {self.pos:#x}, but only {self.remaining} bytes remain")
        (value,) = packer.unpack_from(self.data, self.pos)
        self.pos += packer.size
        return value

    def u8(self) -> int:
        return self.read("u8")

    def u16(self) -> int:
        return self.read("u16")

    def u32(self) -> int:
        return self.read("u32")

    def u64(self) -> int:
        return self.read("u64")

    def s8(self) -> int:
        return self.read("s8")

    def s16(self) -> int:
        return self.read("s16")

    def s32(self) -> int:
        return self.read("s32")

    def s64(self) -> int:
        return self.read("s64")

    def f32(self) -> float:
        return self.read("f32")

    def f64(self) -> float:
        return self.read("f64")


class Writer:
    """Builds big-endian data (or little-endian, with `byteorder="<"`).

    `getvalue()` returns the bytes written so far.
    """

    def __init__(self, byteorder: str = ">") -> None:
        self.byteorder = byteorder
        self._structs = _structs(byteorder)
        self.buffer = bytearray()

    def __len__(self) -> int:
        return len(self.buffer)

    @property
    def pos(self) -> int:
        return len(self.buffer)

    def getvalue(self) -> bytes:
        return bytes(self.buffer)

    def bytes(self, data: bytes) -> None:
        self.buffer += data

    def zeros(self, size: int) -> None:
        if size < 0:
            raise ValueError(f"can't write {size} bytes")
        self.buffer += bytes(size)

    def align(self, alignment: int, fill: int = 0) -> None:
        """Pad with `fill` bytes up to the next multiple of `alignment`."""
        _check_alignment(alignment)
        self.buffer += bytes([fill]) * (-len(self.buffer) % alignment)

    def cstring(self, text: bytes, size: int | None = None) -> None:
        """A NUL-terminated string. With `size`, a fixed-size field padded with NULs."""
        if b"\0" in text:
            raise ValueError("string contains a NUL")
        if size is None:
            self.buffer += text + b"\0"
            return
        if len(text) >= size:
            raise ValueError(f"{len(text)}-byte string doesn't fit a {size}-byte field with its NUL")
        self.buffer += text + bytes(size - len(text))

    def patch(self, offset: int, kind: str, value: float) -> None:
        """Overwrite a value already written, such as an offset known only later."""
        packer = self._structs[kind]
        if offset < 0 or offset + packer.size > len(self.buffer):
            raise ValueError(f"{kind} at {offset:#x} is outside the {len(self.buffer)} bytes written")
        packer.pack_into(self.buffer, offset, value)

    def write(self, kind: str, value: float) -> None:
        self.buffer += self._structs[kind].pack(value)

    def u8(self, value: int) -> None:
        self.write("u8", value)

    def u16(self, value: int) -> None:
        self.write("u16", value)

    def u32(self, value: int) -> None:
        self.write("u32", value)

    def u64(self, value: int) -> None:
        self.write("u64", value)

    def s8(self, value: int) -> None:
        self.write("s8", value)

    def s16(self, value: int) -> None:
        self.write("s16", value)

    def s32(self, value: int) -> None:
        self.write("s32", value)

    def s64(self, value: int) -> None:
        self.write("s64", value)

    def f32(self, value: float) -> None:
        self.write("f32", value)

    def f64(self, value: float) -> None:
        self.write("f64", value)
