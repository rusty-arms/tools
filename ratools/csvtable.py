"""Binary CSV table reader and writer.

Implements specs formats/binary-csv.md, version of 2026-10-28 (specs commit 58c5f07, specs #3).

A binary CSV table is the data of one MST entry of type `csv`: a list of rows, each a text key
and a list of typed fields. `parse` reads one into a `Table`, following every count and offset
rather than assuming the layout, and `Table.pack` writes it back with the spec's layout, so that
`parse(data).pack() == data` for a table that follows the spec. `check` reports which spec
sections a table breaks, and `python3 -m ratools.csvtable check ARCHIVE` runs it on every `csv`
entry of an MST archive.

Not covered, because the spec has them only as leads: field type 3, which is rejected like any
other unknown type, and the meaning of the header word at offset 12, which is kept as a number.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum

from .binary import Reader, ReadError, Writer

HEADER_SIZE = 16
DIRECTORY_ROW_SIZE = 16
FIELD_SIZE = 12
KEY_ALIGN = 4


class CsvError(ValueError):
    """The data isn't a binary CSV table this module can read or write."""


class FieldType(IntEnum):
    STRING = 0
    NUMBER = 1
    WIDE_STRING = 2


@dataclass
class Field:
    """One typed field: a float for a number, text for a string or a wide string."""

    type: FieldType
    value: float | str

    @classmethod
    def number(cls, value: float) -> Field:
        return cls(FieldType.NUMBER, value)

    @classmethod
    def string(cls, value: str) -> Field:
        return cls(FieldType.STRING, value)

    @classmethod
    def wide(cls, value: str) -> Field:
        return cls(FieldType.WIDE_STRING, value)


@dataclass
class Row:
    """A row: its key and its fields. Keys can repeat within a table."""

    key: str
    fields: list[Field] = field(default_factory=list)


@dataclass
class Table:
    rows: list[Row] = field(default_factory=list)
    byteorder: str = ">"
    # The header word at offset 12. Zero in every table observed; a lead calls it flags.
    unknown_12: int = 0

    def find(self, key: str) -> list[Row]:
        """Every row with this key, in order. The spec doesn't say how the game picks one."""
        return [row for row in self.rows if row.key == key]

    def pack(self) -> bytes:
        """The table as bytes, laid out as the spec's "Writing a table" describes."""
        if not self.rows:
            raise CsvError("a table has at least one row")
        out = Writer(self.byteorder)
        out.u32(0)  # table size, filled in at the end
        out.u32(len(self.rows))
        out.u32(HEADER_SIZE)
        out.u32(self.unknown_12)
        out.zeros(DIRECTORY_ROW_SIZE * len(self.rows))

        for index, row in enumerate(self.rows):
            if not row.fields:
                raise CsvError(f"row {index} has no fields")
            if len(row.fields) > 0xFFFF or index > 0xFFFF:
                raise CsvError(f"row {index}: more than 65535 fields or rows")
            key = _encode_text(row.key, f"row {index}'s key")
            if not key or b"\0" in key:
                raise CsvError(f"row {index}'s key is empty or holds a NUL")
            key_offset = out.pos
            out.bytes(key + b"\0")
            out.align(KEY_ALIGN)
            fields_offset = out.pos

            texts = [_encode_field(f, out.byteorder, index, n) for n, f in enumerate(row.fields)]
            text_pos = fields_offset + FIELD_SIZE * len(row.fields)
            for item, text in zip(row.fields, texts, strict=True):
                out.u32(item.type)
                if item.type == FieldType.NUMBER:
                    out.f32(item.value)
                    out.u32(0)
                else:
                    out.u32(text_pos)
                    unit = 2 if item.type == FieldType.WIDE_STRING else 1
                    out.u32(len(text) // unit - 1)
                    text_pos += len(text)
            for text in texts:
                out.bytes(text)

            directory = HEADER_SIZE + DIRECTORY_ROW_SIZE * index
            out.patch(directory, "u32", key_offset)
            out.patch(directory + 4, "u32", len(key))
            out.patch(directory + 8, "u16", len(row.fields))
            out.patch(directory + 10, "u16", index)
            out.patch(directory + 12, "u32", fields_offset)
        out.patch(0, "u32", out.pos)
        return out.getvalue()


def _encode_text(text: str, what: str) -> bytes:
    try:
        return text.encode("ascii")
    except UnicodeEncodeError:
        raise CsvError(f"{what} isn't ASCII") from None


def _encode_field(item: Field, byteorder: str, row: int, index: int) -> bytes:
    """A string field's text with its terminator, or nothing for a number."""
    what = f"row {row}, field {index}"
    if item.type == FieldType.NUMBER:
        if not isinstance(item.value, int | float):
            raise CsvError(f"{what}: a number field needs a number")
        return b""
    if not isinstance(item.value, str):
        raise CsvError(f"{what}: a string field needs text")
    if item.type == FieldType.STRING:
        text = _encode_text(item.value, what)
        if b"\0" in text:
            raise CsvError(f"{what} holds a NUL")
        return text + b"\0"
    if item.type == FieldType.WIDE_STRING:
        text = item.value.encode("utf-16-be" if byteorder == ">" else "utf-16-le")
        if "\0" in item.value:
            raise CsvError(f"{what} holds a zero code unit")
        return text + b"\0\0"
    raise CsvError(f"{what}: unknown field type {item.type}")


def parse(data: bytes, byteorder: str = ">") -> Table:
    """Read a table in the given byte order, which is the MST archive's."""
    try:
        return _parse(Reader(data, byteorder=byteorder))
    except ReadError as error:
        raise CsvError(f"table is cut short: {error}") from None


def _parse(reader: Reader) -> Table:
    reader.u32()  # table size: `check` compares it with the data's length
    row_count = reader.u32()
    directory = reader.u32()
    table = Table(byteorder=reader.byteorder, unknown_12=reader.u32())
    for index in range(row_count):
        reader.seek(directory + DIRECTORY_ROW_SIZE * index)
        key_offset, key_length = reader.u32(), reader.u32()
        field_count = reader.u16()
        reader.u16()  # row index: `check` compares it with the row's position
        fields_offset = reader.u32()
        key = _decode_ascii(reader.sub(key_offset, key_length).bytes(key_length), index)
        row = Row(key)
        for n in range(field_count):
            reader.seek(fields_offset + FIELD_SIZE * n)
            kind = reader.u32()
            if kind == FieldType.NUMBER:
                row.fields.append(Field.number(reader.f32()))
                reader.u32()
                continue
            offset, length = reader.u32(), reader.u32()
            if kind == FieldType.STRING:
                text = reader.sub(offset, length).bytes(length)
                row.fields.append(Field.string(_decode_ascii(text, index)))
            elif kind == FieldType.WIDE_STRING:
                text = reader.sub(offset, 2 * length).bytes(2 * length)
                codec = "utf-16-be" if reader.byteorder == ">" else "utf-16-le"
                try:
                    row.fields.append(Field.wide(text.decode(codec)))
                except UnicodeDecodeError:
                    raise CsvError(f"row {index}, field {n}: wide string isn't UTF-16") from None
            else:
                raise CsvError(f"row {index}, field {n}: unknown field type {kind}")
        table.rows.append(row)
    return table


def _decode_ascii(text: bytes, row: int) -> str:
    try:
        return text.decode("ascii")
    except UnicodeDecodeError:
        raise CsvError(f"row {row}: text isn't ASCII") from None


# The spec's sections, as `check` names them.
SECTIONS = (
    "Header",
    "Row directory",
    "Row keys",
    "Field records",
    "Numbers",
    "Strings",
    "Wide strings",
    "Layout",
    "Round trip",
)


def check(data: bytes, byteorder: str = ">") -> dict[str, list[str]]:
    """Check a table against the spec. Returns each section's problems; empty means it passed.

    The messages give row and field numbers, offsets and counts, never keys or values, so that
    a result on the owner's disc can be shared.
    """
    problems: dict[str, list[str]] = {section: [] for section in SECTIONS}
    try:
        table = parse(data, byteorder)
    except CsvError as error:
        section = "Field records" if "field type" in str(error) else "Header"
        problems[section].append(f"can't parse: {error}")
        return problems
    reader = Reader(data, byteorder=byteorder)
    size, row_count, directory, unknown = (reader.u32() for _ in range(4))
    if size != len(data):
        problems["Header"].append(f"table size {size} isn't the data's length {len(data)}")
    if row_count < 1:
        problems["Header"].append("no rows")
    if directory != HEADER_SIZE:
        problems["Header"].append(f"row directory at {directory}, not 16")
    if unknown:
        problems["Header"].append("the word at offset 12 isn't zero")

    layout = problems["Layout"]
    pos = directory + DIRECTORY_ROW_SIZE * row_count
    for index in range(row_count):
        reader.seek(directory + DIRECTORY_ROW_SIZE * index)
        key_offset, key_length = reader.u32(), reader.u32()
        field_count, row_index, fields_offset = reader.u16(), reader.u16(), reader.u32()
        if key_offset != pos:
            layout.append(f"row {index}: key at {key_offset}, not {pos}")
        pos = fields_offset + FIELD_SIZE * field_count
        if row_index != index:
            problems["Row directory"].append(f"row {index}: index field is {row_index}")
        if not key_length or not field_count:
            problems["Row directory"].append(f"row {index}: empty key or no fields")
        key = data[key_offset : key_offset + key_length]
        if not _printable(key) or key != key.strip(b" "):
            problems["Row keys"].append(f"row {index}: key outside the spec's characters")
        if data[key_offset + key_length : key_offset + key_length + 1] != b"\0":
            problems["Row keys"].append(f"row {index}: no NUL after the key")
        expected = -(-(key_offset + key_length + 1) // KEY_ALIGN) * KEY_ALIGN
        if fields_offset != expected or any(data[key_offset + key_length : fields_offset]):
            problems["Row keys"].append(f"row {index}: padding after the key isn't as specified")
        for n in range(field_count):
            reader.seek(fields_offset + FIELD_SIZE * n)
            kind, second, third = reader.u32(), reader.u32(), reader.u32()
            where = f"row {index}, field {n}"
            if kind == FieldType.NUMBER:
                exponent = second >> 23 & 0xFF
                if exponent == 0xFF or (exponent == 0 and second & 0x7FFFFF):
                    problems["Numbers"].append(f"{where}: not finite, or subnormal")
                if third:
                    problems["Numbers"].append(f"{where}: third word isn't zero")
                continue
            if second != pos:
                layout.append(f"{where}: text at {second}, not {pos}")
            pos = second + (third + 1 if kind == FieldType.STRING else 2 * third + 2)
            if kind == FieldType.STRING:
                text = data[second : second + third + 1]
                if not third or not _printable(text[:-1]) or text[-1:] != b"\0":
                    problems["Strings"].append(f"{where}: not printable ASCII and a NUL")
            else:
                text = table.rows[index].fields[n].value
                end = second + 2 * third
                if data[end : end + 2] != b"\0\0":
                    problems["Wide strings"].append(f"{where}: no zero code unit after the text")
                if any(not (0x20 <= ord(c) <= 0x7E or c == "\n") for c in text):
                    problems["Wide strings"].append(f"{where}: code unit outside the spec's set")

    if pos != len(data):
        layout.append(f"table ends at {len(data)}, not at the last row's end {pos}")

    try:
        if table.pack() != data:
            problems["Round trip"].append("parse then pack doesn't give the same bytes")
    except CsvError as error:
        problems["Round trip"].append(f"can't pack: {error}")
    return problems


def _printable(text: bytes) -> bool:
    return all(0x20 <= byte <= 0x7E for byte in text)


def main(argv: list[str] | None = None) -> int:
    """`python3 -m ratools.csvtable check ARCHIVE`: check every `csv` entry of an MST archive.

    Prints only counts and the spec sections that pass or fail, never names, keys or values.
    """
    import argparse

    from . import mst

    parser = argparse.ArgumentParser(prog="python3 -m ratools.csvtable", description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    check_command = commands.add_parser("check", help="check every csv entry of an MST archive")
    check_command.add_argument("archive", help="path to the MST archive")
    args = parser.parse_args(argv)

    with open(args.archive, "rb") as f:
        data = f.read()
    try:
        archive = mst.parse(data)
    except mst.MstError as error:
        print(f"FAILED: can't read the MST archive ({mst._scrub(error)})")
        return 1
    tables = [entry for entry in archive.entries if entry.type == "csv"]
    failing = {section: 0 for section in SECTIONS}
    first = {}
    for number, entry in enumerate(tables):
        for section, found in check(entry.data, archive.byteorder).items():
            if found:
                failing[section] += 1
                first.setdefault(section, f"table {number}: {found[0]}")
    print(f"{len(tables)} csv tables")
    for section in SECTIONS:
        if failing[section]:
            print(f"FAIL  {section}: {failing[section]} tables, first: {first[section]}")
        else:
            print(f"pass  {section}")
    failed = [section for section in SECTIONS if failing[section]]
    if not tables:
        failed.append("no csv entries")
    print("FAILED: " + ", ".join(failed) if failed else "PASSED")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
