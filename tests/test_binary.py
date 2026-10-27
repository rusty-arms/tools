"""The shared big-endian reader and writer, on made-up bytes."""

import math
import unittest

from ratools.binary import FORMATS, Reader, ReadError, Writer


class ReaderTest(unittest.TestCase):
    def test_big_endian_values(self):
        data = bytes.fromhex("01 0203 04050607 ff fffe 3f800000")
        reader = Reader(data)
        self.assertEqual(reader.u8(), 1)
        self.assertEqual(reader.u16(), 0x0203)
        self.assertEqual(reader.u32(), 0x04050607)
        self.assertEqual(reader.s8(), -1)
        self.assertEqual(reader.s16(), -2)
        self.assertEqual(reader.f32(), 1.0)
        self.assertEqual(reader.remaining, 0)

    def test_reading_past_the_end_fails(self):
        reader = Reader(b"\x00\x01\x02")
        reader.skip(2)
        with self.assertRaisesRegex(ReadError, "0x2"):
            reader.u16()
        self.assertEqual(reader.pos, 2, "a failed read doesn't move")
        with self.assertRaises(ReadError):
            reader.bytes(2)
        with self.assertRaises(ReadError):
            reader.seek(4)
        with self.assertRaises(ReadError):
            reader.skip(-3)

    def test_align(self):
        reader = Reader(bytes(16), offset=5)
        reader.align(4)
        self.assertEqual(reader.pos, 8)
        reader.align(4)
        self.assertEqual(reader.pos, 8, "already aligned")
        reader.align(16)
        self.assertEqual(reader.pos, 16)
        with self.assertRaises(ValueError):
            reader.align(0)

    def test_cstrings(self):
        reader = Reader(b"abc\0xy\0\0\0\0rest")
        self.assertEqual(reader.cstring(), b"abc")
        self.assertEqual(reader.cstring(size=5), b"xy")
        self.assertEqual(reader.pos, 9)
        with self.assertRaisesRegex(ReadError, "no NUL"):
            Reader(b"abc").cstring()

    def test_sub_reader(self):
        reader = Reader(bytes(range(10)))
        sub = reader.sub(4, 3)
        self.assertEqual((len(sub), sub.pos), (3, 0))
        self.assertEqual(sub.bytes(3), b"\x04\x05\x06")
        self.assertEqual(reader.pos, 0, "the parent doesn't move")
        with self.assertRaises(ReadError):
            reader.sub(8, 3)
        with self.assertRaises(ReadError):
            reader.sub(-1, 1)


class WriterTest(unittest.TestCase):
    def test_round_trip_every_kind(self):
        values = {
            "u8": 0xAB,
            "u16": 0xBEEF,
            "u32": 0xDEADBEEF,
            "u64": 2**63 + 5,
            "s8": -128,
            "s16": -12345,
            "s32": -(2**31),
            "s64": -(2**40),
            "f32": -2.5,
            "f64": math.pi,
        }
        self.assertEqual(set(values), set(FORMATS))
        writer = Writer()
        for kind, value in values.items():
            getattr(writer, kind)(value)
        reader = Reader(writer.getvalue())
        for kind, value in values.items():
            self.assertEqual(getattr(reader, kind)(), value, kind)
        self.assertEqual(reader.remaining, 0)

    def test_big_endian_bytes(self):
        writer = Writer()
        writer.u16(0x0102)
        writer.u32(0x03040506)
        self.assertEqual(writer.getvalue(), b"\x01\x02\x03\x04\x05\x06")

    def test_align_and_zeros(self):
        writer = Writer()
        writer.u8(1)
        writer.align(4, fill=0xFF)
        self.assertEqual(writer.getvalue(), b"\x01\xff\xff\xff")
        writer.align(4)
        self.assertEqual(len(writer), 4, "already aligned")
        writer.zeros(2)
        self.assertEqual(writer.pos, 6)

    def test_cstrings(self):
        writer = Writer()
        writer.cstring(b"abc")
        writer.cstring(b"xy", size=5)
        self.assertEqual(writer.getvalue(), b"abc\0xy\0\0\0")
        reader = Reader(writer.getvalue())
        self.assertEqual((reader.cstring(), reader.cstring(size=5)), (b"abc", b"xy"))
        with self.assertRaises(ValueError):
            writer.cstring(b"abcde", size=5)
        with self.assertRaises(ValueError):
            writer.cstring(b"a\0b")

    def test_patch(self):
        writer = Writer()
        writer.u32(0)
        writer.bytes(b"data")
        writer.patch(0, "u32", writer.pos)
        self.assertEqual(Reader(writer.getvalue()).u32(), 8)
        with self.assertRaises(ValueError):
            writer.patch(6, "u32", 0)


if __name__ == "__main__":
    unittest.main()
