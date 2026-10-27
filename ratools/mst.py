"""MST archive reader and writer.

Implements specs formats/mst-archive.md, version of 2026-10-27 (specs commit b2c6c8e).

An MST archive is a 108-byte header, an entry table, a support table of names with no data, and
the entries' data. `parse` reads one into an `Archive`, and `Archive.pack` writes it back. For an
archive that follows the spec, `parse(data).pack() == data`.

The writer lays the data out the way the spec describes: the data offset is the end of the
support table rounded up to 2048, and each entry, in file order, starts at the end of the one
before rounded up to its `align` (32, or 2048 where the original archive used that). Changing an
entry's data moves the entries after it. `check` reports which spec sections an archive breaks.

Not covered, because the spec doesn't settle them: the content word's algorithm (the writer keeps
the stored word, and new entries get 0 unless given one), and the 16- and 24-byte entry names
that the spec has only as a lead for other versions.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from itertools import pairwise

from .binary import Reader, ReadError, Writer

MAGIC = 0x474E4146
HEADER_SIZE = 108
TYPE_VERSION_COUNT = 10
RESERVED_COUNT = 8
ENTRY_NAME_SIZE = 20
ENTRY_ROW_SIZE = ENTRY_NAME_SIZE + 16
SUPPORT_NAME_SIZE = 16
SUPPORT_ROW_SIZE = SUPPORT_NAME_SIZE + 4
DATA_ALIGN = 2048
ENTRY_ALIGN = 32
NAME_CHARACTERS = frozenset("abcdefghijklmnopqrstuvwxyz0123456789 $-_.")


class MstError(ValueError):
    """The data isn't an MST archive this module can read or write."""


def _round_up(value: int, alignment: int) -> int:
    return -(-value // alignment) * alignment


def make_version(flags: int, major: int, minor: int, sub: int) -> int:
    """The version word from its four 8-bit fields."""
    for part in (flags, major, minor, sub):
        if not 0 <= part <= 0xFF:
            raise ValueError(f"version field {part} doesn't fit 8 bits")
    return flags << 24 | major << 16 | minor << 8 | sub


@dataclass
class Entry:
    """A used row of the entry table and its data."""

    name: str
    data: bytes = b""
    timestamp: int = 0
    content_word: int = 0
    # Where the data starts. `parse` sets it, and `pack` updates it. The writer places entries in
    # order of location, and entries without one after the rest, in table order.
    location: int | None = None
    # 2048 where this entry's data was placed on a 2048-byte boundary rather than the next 32.
    align: int = ENTRY_ALIGN
    # The whole name field as read. The spec says to ignore the bytes after the NUL, but the
    # writer puts them back while the name is unchanged, so that a round trip is exact.
    raw_name: bytes | None = field(default=None, repr=False)

    @property
    def type(self) -> str:
        """The part of the name after the dot, which says which loader reads the entry."""
        return self.name.rpartition(".")[2] if "." in self.name else ""


@dataclass
class SupportRow:
    """A used row of the support table: a name and a timestamp, with no data."""

    name: str
    timestamp: int = 0


@dataclass
class Archive:
    byteorder: str = ">"
    version: int = make_version(0x18, 1, 8, 0)
    # The ten per-type version words, kept for the type loaders. Their meaning is a lead only.
    type_versions: list[int] = field(default_factory=lambda: [0] * TYPE_VERSION_COUNT)
    unknown_72: int = 0
    reserved: list[int] = field(default_factory=lambda: [0] * RESERVED_COUNT)
    entries: list[Entry] = field(default_factory=list)
    free_entry_rows: int = 0
    support: list[SupportRow] = field(default_factory=list)
    free_support_rows: int = 0

    @property
    def platform_flags(self) -> int:
        return self.version >> 24

    @property
    def version_tuple(self) -> tuple[int, int, int]:
        """(major, minor, sub)."""
        return (self.version >> 16 & 0xFF, self.version >> 8 & 0xFF, self.version & 0xFF)

    @property
    def data_offset(self) -> int:
        """The end of the support table, rounded up to 2048."""
        rows = len(self.support) + self.free_support_rows
        return _round_up(self._support_offset() + rows * SUPPORT_ROW_SIZE, DATA_ALIGN)

    def _support_offset(self) -> int:
        return HEADER_SIZE + (len(self.entries) + self.free_entry_rows) * ENTRY_ROW_SIZE

    def find(self, name: str) -> Entry | None:
        """The entry called `name`, compared case-insensitively, or None."""
        key = name.lower()
        for entry in self.entries:
            if entry.name.lower() == key:
                return entry
        return None

    def __getitem__(self, name: str) -> Entry:
        entry = self.find(name)
        if entry is None:
            raise KeyError(name)
        return entry

    def file_order(self) -> list[Entry]:
        """The entries in the order the writer places their data."""
        placed = sorted((e for e in self.entries if e.location is not None), key=_location)
        return placed + [e for e in self.entries if e.location is None]

    def pack(self) -> bytes:
        """The archive as bytes. Sets each entry's `location` to where its data went."""
        _check_supported(self.version)
        if len(self.type_versions) != TYPE_VERSION_COUNT:
            raise MstError(f"need {TYPE_VERSION_COUNT} per-type version words")
        if len(self.reserved) != RESERVED_COUNT:
            raise MstError(f"need {RESERVED_COUNT} reserved words")
        _check_names([e.name for e in self.entries], ENTRY_NAME_SIZE, "entry")
        _check_names([s.name for s in self.support], SUPPORT_NAME_SIZE, "support")
        for entry in self.entries:
            if entry.align < ENTRY_ALIGN or entry.align % ENTRY_ALIGN:
                raise MstError(f"{entry.name}: align {entry.align} isn't a multiple of 32")

        # Lay the data out first: the header needs the archive size.
        locations = {}
        end = self.data_offset
        for index, entry in enumerate(self.file_order()):
            start = end if index == 0 else _round_up(end, entry.align)
            locations[id(entry)] = start
            end = start + len(entry.data)
        size = _round_up(end, ENTRY_ALIGN) if self.entries else self.data_offset

        out = Writer(self.byteorder)
        for value in (
            MAGIC,
            self.version,
            size,
            len(self.entries),
            self.free_entry_rows,
            len(self.support),
            self.free_support_rows,
            self.data_offset,
            *self.type_versions,
            self.unknown_72,
            *self.reserved,
        ):
            out.u32(value)

        for entry in self.entries:
            name = entry.name.encode("ascii")
            raw = entry.raw_name
            if raw is not None and len(raw) == ENTRY_NAME_SIZE and raw.split(b"\0", 1)[0] == name:
                out.bytes(raw)
            else:
                out.cstring(name, ENTRY_NAME_SIZE)
            out.u32(locations[id(entry)])
            out.u32(len(entry.data))
            out.u32(entry.timestamp)
            out.u32(entry.content_word)
        out.zeros(self.free_entry_rows * ENTRY_ROW_SIZE)

        for row in self.support:
            out.cstring(row.name.encode("ascii"), SUPPORT_NAME_SIZE)
            out.u32(row.timestamp)
        out.zeros(self.free_support_rows * SUPPORT_ROW_SIZE)

        for entry in self.file_order():
            out.zeros(locations[id(entry)] - out.pos)
            out.bytes(entry.data)
        out.zeros(max(size - out.pos, 0))

        for entry in self.entries:
            entry.location = locations[id(entry)]
        return out.getvalue()


def _location(entry: Entry) -> int:
    return entry.location if entry.location is not None else -1


def _check_supported(version: int) -> None:
    """Only the 20-byte entry name is in the spec as observed; the others are a lead."""
    flags, major, minor = version >> 24, version >> 16 & 0xFF, version >> 8 & 0xFF
    if flags & 0x20 or (major == 1 and minor <= 7):
        raise MstError(
            f"version {major}.{minor} with flags {flags:#04x} may use another entry name width;"
            " the spec has that only as a lead (Entry name width by version)"
        )


def _check_names(names: list[str], size: int, what: str) -> None:
    seen = set()
    for name in names:
        try:
            encoded = name.encode("ascii")
        except UnicodeEncodeError:
            raise MstError(f"{what} name {name!r} isn't ASCII") from None
        if b"\0" in encoded or not encoded:
            raise MstError(f"{what} name {name!r} is empty or holds a NUL")
        if len(encoded) >= size:
            raise MstError(f"{what} name {name!r} doesn't fit {size} bytes with its NUL")
        if name.lower() in seen:
            raise MstError(f"two {what} rows are called {name!r}, ignoring case")
        seen.add(name.lower())


def detect_byteorder(data: bytes) -> str:
    """The byte order that makes the magic come out right: '>' for GNAF, '<' for FANG."""
    head = bytes(data[:4])
    if head == MAGIC.to_bytes(4, "big"):
        return ">"
    if head == MAGIC.to_bytes(4, "little"):
        return "<"
    raise MstError(f"not an MST archive: magic {head.hex()}")


def _name(raw: bytes) -> str:
    text = raw.split(b"\0", 1)[0]
    try:
        return text.decode("ascii")
    except UnicodeDecodeError:
        raise MstError(f"name {text!r} isn't ASCII") from None


def parse(data: bytes) -> Archive:
    """Read an archive. Raises `MstError` if it can't be read; see `check` for the spec rules."""
    byteorder = detect_byteorder(data)
    try:
        return _parse(Reader(data, byteorder=byteorder))
    except ReadError as error:
        raise MstError(f"archive is cut short: {error}") from None


def _parse(reader: Reader) -> Archive:
    reader.u32()  # magic, checked already
    version = reader.u32()
    _check_supported(version)
    reader.u32()  # archive size: `check` compares it with the file's length
    entry_count, free_entries = reader.u32(), reader.u32()
    support_count, free_support = reader.u32(), reader.u32()
    reader.u32()  # data offset: the writer works it out again, and `check` compares the two
    archive = Archive(
        byteorder=reader.byteorder,
        version=version,
        type_versions=[reader.u32() for _ in range(TYPE_VERSION_COUNT)],
        unknown_72=reader.u32(),
        reserved=[reader.u32() for _ in range(RESERVED_COUNT)],
        free_entry_rows=free_entries,
        free_support_rows=free_support,
    )

    rows = []
    for index in range(entry_count):
        raw = reader.bytes(ENTRY_NAME_SIZE)
        location, length, timestamp, content_word = (reader.u32() for _ in range(4))
        if location + length > len(reader):
            raise MstError(
                f"entry row {index}: data at {location:#x} runs past the end of the file"
            )
        entry = Entry(
            name=_name(raw),
            data=reader.data[location : location + length],
            timestamp=timestamp,
            content_word=content_word,
            location=location,
            raw_name=raw,
        )
        rows.append(entry)
    archive.entries = rows
    reader.skip(free_entries * ENTRY_ROW_SIZE)

    for _ in range(support_count):
        name = _name(reader.bytes(SUPPORT_NAME_SIZE))
        archive.support.append(SupportRow(name, reader.u32()))

    # An entry that doesn't start at the next multiple of 32 after the previous one was placed on
    # a 2048-byte boundary; keep that so the writer places it the same way.
    end = None
    for entry in archive.file_order():
        if (
            end is not None
            and entry.location != _round_up(end, ENTRY_ALIGN)
            and entry.location == _round_up(end, DATA_ALIGN)
        ):
            entry.align = DATA_ALIGN
        end = entry.location + len(entry.data)
    return archive


# The spec's sections, as `check` names them.
SECTIONS = (
    "Magic and byte order",
    "Header",
    "Entry table",
    "Entry row",
    "Entry names",
    "Support table",
    "Data region",
    "Entry data placement",
    "Round trip",
)


def check(data: bytes) -> dict[str, list[str]]:
    """Check an archive against the spec. Returns each section's problems; empty means it passed.

    The messages give offsets and counts but never names or contents, so that a result on the
    owner's disc can be shared.
    """
    problems: dict[str, list[str]] = {section: [] for section in SECTIONS}
    try:
        archive = parse(data)
    except MstError as error:
        section = "Magic and byte order" if "magic" in str(error) else "Header"
        problems[section].append(f"can't parse: {_scrub(error)}")
        return problems
    reader = Reader(data, byteorder=archive.byteorder)

    reader.seek(8)
    size = reader.u32()
    reader.seek(28)
    data_offset = reader.u32()
    if size != len(data):
        problems["Header"].append(f"archive size {size} isn't the file's length {len(data)}")

    table_end = archive._support_offset()
    unused = data[HEADER_SIZE + len(archive.entries) * ENTRY_ROW_SIZE : table_end]
    if any(unused):
        problems["Entry table"].append("an unused entry row isn't all zero")

    spans = sorted((e.location, e.location + len(e.data)) for e in archive.entries)
    for (start, end), (next_start, _) in pairwise(spans):
        if next_start == start:
            problems["Entry row"].append(f"two entries share location {start:#x}")
        elif next_start < end:
            problems["Entry row"].append(f"entries at {start:#x} and {next_start:#x} overlap")
    for start, _ in spans:
        if start < data_offset:
            problems["Entry row"].append(f"entry at {start:#x} is before the data offset")

    problems["Entry names"] = _name_problems([e.name for e in archive.entries], "entry")

    support_end = table_end + (len(archive.support) + archive.free_support_rows) * SUPPORT_ROW_SIZE
    for index in range(len(archive.support)):
        field_ = data[table_end + index * SUPPORT_ROW_SIZE :][:SUPPORT_NAME_SIZE]
        if any(field_.partition(b"\0")[2]):
            problems["Support table"].append(f"support row {index}: bytes after the NUL")
    if any(data[table_end + len(archive.support) * SUPPORT_ROW_SIZE : support_end]):
        problems["Support table"].append("an unused support row isn't all zero")
    problems["Support table"] += _name_problems([s.name for s in archive.support], "support")

    if data_offset != archive.data_offset:
        problems["Data region"].append(
            f"data offset {data_offset:#x}, but the support table ends at {support_end:#x}"
        )
    if any(data[support_end:data_offset]):
        problems["Data region"].append("bytes before the data offset aren't all zero")
    if spans and spans[0][0] != data_offset:
        problems["Data region"].append(f"first entry's data is at {spans[0][0]:#x}")

    placement = problems["Entry data placement"]
    end = None
    for start, stop in spans:
        if start % ENTRY_ALIGN:
            placement.append(f"entry at {start:#x} isn't on a multiple of 32")
        if end is not None:
            if start not in (_round_up(end, ENTRY_ALIGN), _round_up(end, DATA_ALIGN)):
                placement.append(f"entry at {start:#x} after data ending at {end:#x}")
            if start > end and any(data[end:start]):
                placement.append(f"padding before {start:#x} isn't all zero")
        end = stop
    if end is not None:
        if len(data) != _round_up(end, ENTRY_ALIGN):
            placement.append(f"file ends at {len(data)}, not the last entry's end rounded to 32")
        if any(data[end:]):
            placement.append("padding at the end isn't all zero")

    try:
        if archive.pack() != data:
            problems["Round trip"].append("parse then pack doesn't give the same bytes")
    except MstError as error:
        problems["Round trip"].append(f"can't pack: {_scrub(error)}")
    return problems


def _name_problems(names: list[str], what: str) -> list[str]:
    """The spec's observed name rules. Messages give row numbers, never the names."""
    problems = []
    seen = set()
    for index, name in enumerate(names):
        base, dot, kind = name.partition(".")
        if name != name.lower() or not set(name) <= NAME_CHARACTERS:
            problems.append(f"{what} row {index}: characters outside the spec's set")
        if not dot or "." in kind or not base or not (2 <= len(kind) <= 3 and kind.isalpha()):
            problems.append(f"{what} row {index}: not one dot and a 2- or 3-letter type")
        if name.lower() in seen:
            problems.append(f"{what} row {index}: name used twice")
        seen.add(name.lower())
    return problems


def _scrub(error: Exception) -> str:
    """An error message without any name in it."""
    text = str(error)
    for quote in ("'", '"'):
        if text.count(quote) >= 2:
            head, _, rest = text.partition(quote)
            text = head + "<name>" + rest.rpartition(quote)[2]
    return text


def main(argv: list[str] | None = None) -> int:
    """`python3 -m ratools.mst check ARCHIVE`: check an archive against the spec.

    Prints only counts, offsets and the spec sections that pass or fail, never names or data.
    """
    import argparse

    parser = argparse.ArgumentParser(
        prog="python3 -m ratools.mst", description=__doc__.split("\n")[0]
    )
    commands = parser.add_subparsers(dest="command", required=True)
    check_command = commands.add_parser("check", help="check an archive against the MST spec")
    check_command.add_argument("archive", help="path to the archive")
    args = parser.parse_args(argv)

    with open(args.archive, "rb") as f:
        data = f.read()
    problems = check(data)
    try:
        archive = parse(data)
        print(f"{len(archive.entries)} entries, {len(archive.support)} support rows")
        parsed = True
    except MstError:
        parsed = False
    for section in SECTIONS:
        found = problems[section]
        print(f"{'FAIL' if found else 'pass' if parsed else 'skip'}  {section}")
        for message in found[:5]:
            print(f"      {message}")
        if len(found) > 5:
            print(f"      and {len(found) - 5} more")
    failed = [section for section in SECTIONS if problems[section]]
    print("FAILED: " + ", ".join(failed) if failed else "PASSED")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
