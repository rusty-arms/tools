"""The binary CSV table reader and writer, on synthetic tables built here from the spec.

Every table is made up. `build` lays one out by hand with `struct`, independently of the writer,
following specs formats/binary-csv.md; keys and values are invented.
"""

import contextlib
import io
import os
import struct
import tempfile
import unittest

from ratools import csvtable, mst
from ratools.csvtable import Field, FieldType, Row, Table


def build(rows, order=">", unknown=0):
    """A synthetic table. `rows` holds (key, fields); a field is (type, value), where a number's
    value is its 32-bit pattern as an int, or a float."""
    u32 = struct.Struct(order + "I").pack
    u16 = struct.Struct(order + "H").pack
    wide = "utf-16-be" if order == ">" else "utf-16-le"
    body = bytearray()
    directory = []
    start = 16 + 16 * len(rows)
    for key, fields in rows:
        key_offset = start + len(body)
        body += key + b"\0"
        while (start + len(body)) % 4:
            body += b"\0"
        fields_offset = start + len(body)
        text_pos = fields_offset + 12 * len(fields)
        records, texts = b"", b""
        for kind, value in fields:
            if kind == 1:
                bits = (
                    value
                    if isinstance(value, int)
                    else struct.unpack(">I", struct.pack(">f", value))[0]
                )
                records += u32(1) + u32(bits) + u32(0)
                continue
            text = value.encode("ascii") + b"\0" if kind == 0 else value.encode(wide) + b"\0\0"
            length = len(value)
            records += u32(kind) + u32(text_pos) + u32(length)
            texts += text
            text_pos += len(text)
        body += records + texts
        directory.append((key_offset, len(key), len(fields), fields_offset))
    out = bytearray(u32(start + len(body)) + u32(len(rows)) + u32(16) + u32(unknown))
    for index, (key_offset, key_length, count, fields_offset) in enumerate(directory):
        out += u32(key_offset) + u32(key_length) + u16(count) + u16(index) + u32(fields_offset)
    return bytes(out + body)


def spec_example():
    """The made-up example in the spec's Example section."""
    return build([(b"speed", [(1, 2.5), (0, "fast")]), (b"label", [(2, "Hi")])])


def failing(data, order=">"):
    return {section for section, found in csvtable.check(data, order).items() if found}


class SpecExampleTest(unittest.TestCase):
    def test_bytes_match_the_spec(self):
        data = spec_example()
        self.assertEqual(len(data), 110)
        u32 = lambda offset: struct.unpack_from(">I", data, offset)[0]
        self.assertEqual([u32(0), u32(4), u32(8), u32(12)], [110, 2, 16, 0])
        self.assertEqual(struct.unpack_from(">IIHHI", data, 16), (48, 5, 2, 0, 56))
        self.assertEqual(struct.unpack_from(">IIHHI", data, 32), (85, 5, 1, 1, 92))
        self.assertEqual(data[48:56], b"speed\0\0\0")
        self.assertEqual(data[56:68], bytes.fromhex("00000001 40200000 00000000"))
        self.assertEqual(data[68:80], struct.pack(">III", 0, 80, 4))
        self.assertEqual(data[80:92], b"fast\0label\0\0")
        self.assertEqual(data[92:110], struct.pack(">III", 2, 104, 2) + b"\0H\0i\0\0")

    def test_parse(self):
        table = csvtable.parse(spec_example())
        self.assertEqual(
            table.rows,
            [
                Row("speed", [Field.number(2.5), Field.string("fast")]),
                Row("label", [Field.wide("Hi")]),
            ],
        )
        self.assertEqual(table.unknown_12, 0)

    def test_round_trip_and_check(self):
        data = spec_example()
        self.assertEqual(csvtable.parse(data).pack(), data)
        self.assertEqual(failing(data), set())

    def test_writer_builds_the_example(self):
        table = Table(
            [
                Row("speed", [Field.number(2.5), Field.string("fast")]),
                Row("label", [Field.wide("Hi")]),
            ]
        )
        self.assertEqual(table.pack(), spec_example())


class ReaderTest(unittest.TestCase):
    def test_mixed_rows_and_types(self):
        rows = [
            (b"a", [(1, 1.0), (0, "x"), (2, "y")]),
            (b"bb", [(0, "one")]),
            (b"ccc", [(2, "w"), (1, -3.25), (1, 7.0), (0, "zz")]),
        ]
        data = build(rows)
        table = csvtable.parse(data)
        self.assertEqual([len(row.fields) for row in table.rows], [3, 1, 4])
        self.assertEqual(
            [f.type for f in table.rows[2].fields],
            [FieldType.WIDE_STRING, FieldType.NUMBER, FieldType.NUMBER, FieldType.STRING],
        )
        self.assertEqual(table.pack(), data)
        self.assertEqual(failing(data), set())

    def test_wide_string_at_an_odd_offset(self):
        # A 4-byte string and its NUL, after the row's aligned field records, leave the wide
        # string that follows at an odd offset.
        data = build([(b"k", [(0, "ab")]), (b"m", [(0, "odds"), (2, "Wide\ntext")])])
        table = csvtable.parse(data)
        offset = struct.unpack_from(">I", data, table_field_record(data, 1, 1) + 4)[0]
        self.assertEqual(offset % 2, 1)
        self.assertEqual(table.rows[1].fields[1].value, "Wide\ntext")
        self.assertEqual(table.pack(), data)
        self.assertEqual(failing(data), set())

    def test_empty_wide_string(self):
        data = build([(b"e", [(2, "")])])
        self.assertEqual(csvtable.parse(data).rows[0].fields[0].value, "")
        self.assertEqual(csvtable.parse(data).pack(), data)
        self.assertEqual(failing(data), set())

    def test_duplicate_keys_are_kept_in_order(self):
        data = build([(b"dup", [(1, 1.0)]), (b"other", [(1, 2.0)]), (b"dup", [(1, 3.0)])])
        table = csvtable.parse(data)
        self.assertEqual([row.key for row in table.rows], ["dup", "other", "dup"])
        self.assertEqual([row.fields[0].value for row in table.find("dup")], [1.0, 3.0])
        self.assertEqual(table.pack(), data)

    def test_numbers_keep_their_exact_bits(self):
        patterns = [0x80000000, 0x3DCCCCCD, 0xC2F6E979, 0x7F7FFFFF, 0x00800000, 0x4B000001]
        data = build([(b"n", [(1, bits) for bits in patterns])])
        table = csvtable.parse(data)
        self.assertEqual(str(table.rows[0].fields[0].value), "-0.0")
        self.assertEqual(table.pack(), data)
        self.assertEqual(failing(data), set())

    def test_little_endian(self):
        rows = [(b"le", [(1, 0.5), (2, "Hi"), (0, "s")])]
        data = build(rows, order="<")
        table = csvtable.parse(data, "<")
        self.assertEqual([f.value for f in table.rows[0].fields], [0.5, "Hi", "s"])
        self.assertEqual(table.pack(), data)
        self.assertEqual(Table(table.rows, byteorder="<").pack(), data)
        self.assertEqual(failing(data, "<"), set())

    def test_follows_offsets_not_the_layout(self):
        # Same rows, but with the second row's key and fields moved to the end of the table.
        data = bytearray(build([(b"a", [(1, 1.0)]), (b"b", [(0, "q")])]))
        moved_key, moved_fields = len(data), len(data) + 4
        record = struct.pack(">III", 0, moved_fields + 12, 1)
        data += b"b\0\0\0" + record + b"q\0"
        struct.pack_into(">I", data, 32, moved_key)
        struct.pack_into(">I", data, 44, moved_fields)
        struct.pack_into(">I", data, 0, len(data))
        table = csvtable.parse(bytes(data))
        self.assertEqual(table.rows[1], Row("b", [Field.string("q")]))
        self.assertIn("Layout", failing(bytes(data)))

    def test_unknown_field_type_is_rejected(self):
        data = bytearray(spec_example())
        struct.pack_into(">I", data, 56, 3)
        with self.assertRaisesRegex(csvtable.CsvError, "unknown field type 3"):
            csvtable.parse(bytes(data))
        self.assertEqual(failing(bytes(data)), {"Field records"})

    def test_truncated_table(self):
        with self.assertRaisesRegex(csvtable.CsvError, "cut short"):
            csvtable.parse(spec_example()[:100])

    def test_non_ascii_text_is_rejected(self):
        data = bytearray(spec_example())
        data[80] = 0xE9
        with self.assertRaisesRegex(csvtable.CsvError, "ASCII"):
            csvtable.parse(bytes(data))


def table_field_record(data, row, field):
    fields_offset = struct.unpack_from(">I", data, 16 + 16 * row + 12)[0]
    return fields_offset + 12 * field


class WriterTest(unittest.TestCase):
    def test_editing_a_field_moves_what_follows(self):
        table = csvtable.parse(spec_example())
        table.rows[0].fields[1] = Field.string("much faster")
        again = csvtable.parse(table.pack())
        self.assertEqual(again.rows[0].fields[1].value, "much faster")
        self.assertEqual(again.rows[1].fields[0].value, "Hi")
        self.assertEqual(failing(table.pack()), set())

    def test_writer_refuses_what_the_format_cant_hold(self):
        bad = [
            Table([]),
            Table([Row("k", [])]),
            Table([Row("", [Field.number(1)])]),
            Table([Row("k\0", [Field.number(1)])]),
            Table([Row("ké", [Field.number(1)])]),
            Table([Row("k", [Field.string("a\0b")])]),
            Table([Row("k", [Field.string(5)])]),
            Table([Row("k", [Field.number("5")])]),
            Table([Row("k", [Field.wide("a\0")])]),
            Table([Row("k", [Field(3, 0)])]),
        ]
        for table in bad:
            with self.assertRaises(csvtable.CsvError, msg=table):
                table.pack()


class CheckTest(unittest.TestCase):
    """`check` names the spec section each problem breaks, and never prints a key or value."""

    def patched(self, offset, value, data=None):
        data = bytearray(data or spec_example())
        data[offset : offset + len(value)] = value
        return bytes(data)

    def test_size_field(self):
        self.assertEqual(failing(self.patched(0, struct.pack(">I", 111))), {"Header", "Round trip"})

    def test_unknown_header_word(self):
        data = self.patched(12, struct.pack(">I", 1))
        self.assertEqual(failing(data), {"Header"})
        self.assertEqual(csvtable.parse(data).pack(), data, "the word is kept")

    def test_row_index(self):
        data = self.patched(42, struct.pack(">H", 5))
        self.assertEqual(failing(data), {"Row directory", "Round trip"})

    def test_dirty_key_padding(self):
        self.assertEqual(failing(self.patched(54, b"\x01")), {"Row keys", "Round trip"})

    def test_key_characters(self):
        data = build([(b" lead", [(1, 1.0)])])
        self.assertEqual(failing(data), {"Row keys"})

    def test_number_third_word(self):
        self.assertEqual(failing(self.patched(64, b"\0\0\0\x01")), {"Numbers", "Round trip"})

    def test_non_finite_and_subnormal_numbers(self):
        for bits in (0x7F800000, 0x7FC00000, 0x00000001):
            data = build([(b"n", [(1, bits)])])
            self.assertIn("Numbers", failing(data), hex(bits))

    def test_string_rules(self):
        self.assertEqual(failing(build([(b"s", [(0, "")])])), {"Strings"})
        self.assertEqual(failing(build([(b"s", [(0, "tab\there")])])), {"Strings"})

    def test_wide_string_rules(self):
        self.assertEqual(failing(build([(b"w", [(2, "tab\there")])])), {"Wide strings"})
        data = self.patched(108, b"\0\x01")
        self.assertIn("Wide strings", failing(data))

    def test_trailing_padding(self):
        data = spec_example() + b"\0\0"
        data = self.patched(0, struct.pack(">I", len(data)), data)
        self.assertEqual(failing(data), {"Layout", "Round trip"})

    def test_messages_never_hold_keys_or_values(self):
        data = build(
            [(b"SECRETKEY ", [(0, "secretvalue\t"), (2, "secretwide\t"), (1, 0x7F800000)])]
        )
        problems = csvtable.check(data)
        self.assertTrue(problems["Row keys"] and problems["Strings"] and problems["Wide strings"])
        self.assertNotIn("secret", repr(problems).lower())

    def test_cli_checks_every_csv_entry_of_an_archive(self):
        good = mst.Archive(
            entries=[
                mst.Entry("one.csv", spec_example(), 1, 1),
                mst.Entry("pic.tga", b"not a table", 1, 1),
                mst.Entry("two.csv", build([(b"k", [(1, 1.0)])]), 1, 1),
            ]
        )
        bad = mst.Archive(entries=[mst.Entry("one.csv", self.patched(54, b"\x01"), 1, 1)])
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "synthetic.mst")
            for archive, code, last in ((good, 0, "PASSED"), (bad, 1, "FAILED")):
                with open(path, "wb") as f:
                    f.write(archive.pack())
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    self.assertEqual(csvtable.main(["check", path]), code)
                lines = out.getvalue().splitlines()
                self.assertTrue(lines[-1].startswith(last), lines)
                self.assertNotIn("one.csv", out.getvalue())
            self.assertIn("1 csv tables", lines[0])
            with open(path, "wb") as f:
                f.write(b"junk")
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(csvtable.main(["check", path]), 1)


if __name__ == "__main__":
    unittest.main()
