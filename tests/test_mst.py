"""The MST archive reader and writer, on synthetic archives built here from the spec.

Every archive is made up. `build` lays one out by hand with `struct`, independently of the
writer, following specs formats/mst-archive.md; names and data are invented.
"""

import struct
import unittest

from ratools import mst


def build(
    entries,
    support=(),
    free_entries=0,
    free_support=0,
    order=">",
    version=0x18010800,
    words=None,
):
    """A synthetic archive.

    `entries` holds (name field, data, timestamp, content word, gap) in table order; the data goes
    into the file in that same order. `gap` is None to start at the next multiple of 32, or 2048
    to start at the next multiple of 2048. `words` replaces header words 32 to 107.
    """
    u32 = struct.Struct(order + "I").pack
    support_start = 108 + 36 * (len(entries) + free_entries)
    support_end = support_start + 20 * (len(support) + free_support)
    data_offset = -(-support_end // 2048) * 2048

    locations, end = [], data_offset
    for index, (_, data, _, _, gap) in enumerate(entries):
        start = end if index == 0 else -(-end // (gap or 32)) * (gap or 32)
        locations.append(start)
        end = start + len(data)
    size = -(-end // 32) * 32 if entries else data_offset

    out = bytearray(u32(0x474E4146) + u32(version) + u32(size))
    out += u32(len(entries)) + u32(free_entries) + u32(len(support)) + u32(free_support)
    out += u32(data_offset)
    out += words if words is not None else bytes(76)
    for (name, data, timestamp, word, _), location in zip(entries, locations, strict=True):
        out += name.ljust(20, b"\0") + u32(location) + u32(len(data)) + u32(timestamp) + u32(word)
    out += bytes(36 * free_entries)
    for name, timestamp in support:
        out += name.ljust(16, b"\0") + u32(timestamp)
    out += bytes(20 * free_support)
    for (_, data, _, _, _), location in zip(entries, locations, strict=True):
        out += bytes(location - len(out)) + data
    out += bytes(size - len(out))
    return bytes(out)


def spec_example():
    """The made-up example in the spec's Example section, byte for byte."""
    return build(
        [
            (b"alpha.csv", bytes(range(70)), 1000000000, 0x12345678, None),
            (b"beta.tga", b"\xab" * 32, 1000000000, 0x9ABCDEF0, None),
        ],
        free_entries=1,
        free_support=1,
    )


def assert_passes(test, data):
    problems = mst.check(data)
    test.assertEqual({k: v for k, v in problems.items() if v}, {})


class SpecExampleTest(unittest.TestCase):
    def test_layout_matches_the_spec(self):
        data = spec_example()
        self.assertEqual(len(data), 2176)
        self.assertEqual(data[:4], b"GNAF")
        archive = mst.parse(data)
        self.assertEqual(archive.byteorder, ">")
        self.assertEqual(archive.platform_flags, 0x18)
        self.assertEqual(archive.version_tuple, (1, 8, 0))
        self.assertEqual((archive.free_entry_rows, archive.free_support_rows), (1, 1))
        self.assertEqual(archive.data_offset, 2048)
        alpha, beta = archive.entries
        self.assertEqual((alpha.name, alpha.location, len(alpha.data)), ("alpha.csv", 2048, 70))
        self.assertEqual((beta.name, beta.location, len(beta.data)), ("beta.tga", 2144, 32))
        self.assertEqual((alpha.timestamp, alpha.content_word), (1000000000, 0x12345678))
        self.assertEqual(beta.content_word, 0x9ABCDEF0)
        self.assertEqual(alpha.data, bytes(range(70)))
        self.assertEqual((alpha.type, beta.type), ("csv", "tga"))

    def test_round_trip(self):
        data = spec_example()
        self.assertEqual(mst.parse(data).pack(), data)
        assert_passes(self, data)

    def test_writer_builds_the_example(self):
        archive = mst.Archive(free_entry_rows=1, free_support_rows=1)
        archive.entries = [
            mst.Entry("alpha.csv", bytes(range(70)), 1000000000, 0x12345678),
            mst.Entry("beta.tga", b"\xab" * 32, 1000000000, 0x9ABCDEF0),
        ]
        self.assertEqual(archive.pack(), spec_example())
        self.assertEqual([e.location for e in archive.entries], [2048, 2144])


class ReaderTest(unittest.TestCase):
    def test_little_endian_magic(self):
        entries = [(b"one.csv", b"1234567", 7, 9, None)]
        data = build(entries, order="<")
        self.assertEqual(data[:4], b"FANG")
        archive = mst.parse(data)
        self.assertEqual(archive.byteorder, "<")
        self.assertEqual((archive.entries[0].timestamp, archive.entries[0].content_word), (7, 9))
        self.assertEqual(archive.version_tuple, (1, 8, 0))
        self.assertEqual(archive.pack(), data)
        assert_passes(self, data)

    def test_bad_magic(self):
        data = bytearray(spec_example())
        data[:4] = b"NOPE"
        with self.assertRaisesRegex(mst.MstError, "magic"):
            mst.parse(bytes(data))
        self.assertTrue(mst.check(bytes(data))["Magic and byte order"])

    def test_name_stops_at_the_first_nul(self):
        name_field = b"left.sma\0leftover!!"
        data = build([(name_field, b"x" * 40, 1, 2, None)])
        archive = mst.parse(data)
        self.assertEqual(archive.entries[0].name, "left.sma")
        self.assertIs(archive.find("left.sma"), archive.entries[0])
        self.assertEqual(archive.pack(), data, "the leftover bytes survive a round trip")
        assert_passes(self, data)

    def test_renaming_drops_the_leftover_bytes(self):
        archive = mst.parse(build([(b"left.sma\0leftover!!", b"x", 1, 2, None)]))
        archive.entries[0].name = "right.sma"
        again = mst.parse(archive.pack())
        self.assertEqual(again.entries[0].raw_name, b"right.sma".ljust(20, b"\0"))

    def test_skips_free_rows_and_support_table(self):
        entries = [(b"a.ape", b"A" * 33, 1, 1, None), (b"b.ape", b"B" * 5, 2, 2, None)]
        support = [(b"src1.zz", 11), (b"src2.zz", 12), (b"src3.zz", 13)]
        data = build(entries, support, free_entries=40, free_support=37)
        archive = mst.parse(data)
        self.assertEqual([e.data for e in archive.entries], [b"A" * 33, b"B" * 5])
        self.assertEqual([(s.name, s.timestamp) for s in archive.support][1], ("src2.zz", 12))
        self.assertEqual(archive.data_offset, 4096)
        self.assertEqual(archive.pack(), data)
        assert_passes(self, data)

    def test_finds_entries_whatever_the_table_order(self):
        # Data in the file is in the reverse of table order.
        table = [
            (b"zeta.mtx", b"z" * 3, 1, 1),
            (b"mid.fnt", b"m" * 50, 2, 0),
            (b"aa.cam", b"a", 3, 0),
        ]
        archive = mst.parse(self._reversed_data(table))
        self.assertEqual([e.name for e in archive.entries], ["zeta.mtx", "mid.fnt", "aa.cam"])
        self.assertEqual(archive["MID.FNT"].data, b"m" * 50, "names match case-insensitively")
        self.assertEqual(archive.find("aa.cam").data, b"a")
        self.assertIsNone(archive.find("missing.csv"))
        with self.assertRaises(KeyError):
            archive["missing.csv"]
        self.assertEqual([e.name for e in archive.file_order()], ["aa.cam", "mid.fnt", "zeta.mtx"])

    def test_table_order_and_file_order_round_trip(self):
        table = [
            (b"zeta.mtx", b"z" * 3, 1, 1),
            (b"mid.fnt", b"m" * 50, 2, 0),
            (b"aa.cam", b"a", 3, 0),
        ]
        data = self._reversed_data(table)
        self.assertEqual(mst.parse(data).pack(), data)
        assert_passes(self, data)

    @staticmethod
    def _reversed_data(table):
        """Build in file order, then swap the table rows back into `table` order."""
        data = bytearray(build([(*row, None) for row in reversed(table)]))
        rows = [data[108 + 36 * i : 144 + 36 * i] for i in range(len(table))]
        data[108 : 108 + 36 * len(table)] = b"".join(reversed(rows))
        return bytes(data)

    def test_2048_placement_round_trips(self):
        entries = [
            (b"a.csv", b"c" * 100, 1, 1, None),
            (b"b.tga", b"t" * 64, 1, 1, 2048),
            (b"c.tga", b"u" * 10, 1, 1, None),
        ]
        data = build(entries)
        archive = mst.parse(data)
        self.assertEqual([e.location for e in archive.entries], [2048, 4096, 4160])
        self.assertEqual([e.align for e in archive.entries], [32, 2048, 32])
        self.assertEqual(archive.pack(), data)
        assert_passes(self, data)

    def test_header_words_are_kept(self):
        words = struct.pack(">19I", *range(1, 11), 3, *range(100, 108))
        data = build([(b"a.gt", b"g", 1, 0, None)], words=words)
        archive = mst.parse(data)
        self.assertEqual(archive.type_versions, list(range(1, 11)))
        self.assertEqual(archive.unknown_72, 3)
        self.assertEqual(archive.reserved, list(range(100, 108)))
        self.assertEqual(archive.pack(), data)

    def test_empty_archive(self):
        data = build([], free_entries=2, free_support=2)
        archive = mst.parse(data)
        self.assertEqual((archive.entries, archive.support), ([], []))
        self.assertEqual(archive.pack(), data)

    def test_truncated_archive(self):
        data = spec_example()
        with self.assertRaisesRegex(mst.MstError, "past the end"):
            mst.parse(data[:2100])
        with self.assertRaisesRegex(mst.MstError, "cut short"):
            mst.parse(data[:50])

    def test_other_name_widths_are_refused(self):
        for version in (0x10010700, 0x20010800):
            data = build([(b"a.csv", b"x", 1, 1, None)], version=version)
            with self.assertRaisesRegex(mst.MstError, "lead"):
                mst.parse(data)


class WriterTest(unittest.TestCase):
    def test_growing_an_entry_moves_the_ones_after_it(self):
        entries = [(b"a.csv", b"c" * 10, 1, 1, None), (b"b.tga", b"t" * 64, 1, 1, 2048)]
        entries.append((b"c.sma", b"s" * 7, 1, 1, None))
        archive = mst.parse(build(entries))
        archive["a.csv"].data = b"C" * 3000
        data = archive.pack()
        again = mst.parse(data)
        self.assertEqual([e.location for e in again.entries], [2048, 6144, 6208])
        self.assertEqual(again["a.csv"].data, b"C" * 3000)
        self.assertEqual(again["c.sma"].data, b"s" * 7)
        self.assertEqual([e.location for e in archive.entries], [2048, 6144, 6208])
        assert_passes(self, data)

    def test_adding_entries_and_support_rows(self):
        archive = mst.parse(spec_example())
        archive.entries.append(mst.Entry("gamma.wld", b"w" * 5, timestamp=5))
        archive.free_entry_rows = 0
        archive.support.append(mst.SupportRow("src.zz", 9))
        archive.free_support_rows = 0
        data = archive.pack()
        again = mst.parse(data)
        self.assertEqual(again["gamma.wld"].location, 2176, "new entries go after the rest")
        self.assertEqual(again["gamma.wld"].content_word, 0)
        self.assertEqual([(s.name, s.timestamp) for s in again.support], [("src.zz", 9)])
        self.assertEqual(again.pack(), data)
        assert_passes(self, data)

    def test_writer_refuses_bad_names(self):
        for names in (["x" * 16 + ".csv"], ["a.csv", "A.CSV"], ["café.csv"], [""], ["a\0.csv"]):
            archive = mst.Archive(entries=[mst.Entry(name) for name in names])
            with self.assertRaises(mst.MstError, msg=names):
                archive.pack()
        archive = mst.Archive(support=[mst.SupportRow("x" * 13 + ".zz")])
        with self.assertRaisesRegex(mst.MstError, "16 bytes"):
            archive.pack()

    def test_writer_refuses_bad_alignment_and_header_lists(self):
        with self.assertRaisesRegex(mst.MstError, "align"):
            mst.Archive(entries=[mst.Entry("a.csv", align=48)]).pack()
        with self.assertRaisesRegex(mst.MstError, "per-type"):
            mst.Archive(type_versions=[1]).pack()
        with self.assertRaisesRegex(mst.MstError, "reserved"):
            mst.Archive(reserved=[]).pack()

    def test_little_endian_writer(self):
        archive = mst.Archive(byteorder="<", entries=[mst.Entry("a.csv", b"abc", 1, 2)])
        data = archive.pack()
        self.assertEqual(data[:4], b"FANG")
        self.assertEqual(data, build([(b"a.csv", b"abc", 1, 2, None)], order="<"))

    def test_make_version(self):
        self.assertEqual(mst.make_version(0x18, 1, 8, 0), 0x18010800)
        with self.assertRaises(ValueError):
            mst.make_version(256, 1, 8, 0)


class CheckTest(unittest.TestCase):
    """`check` names the spec section each problem breaks, and never prints a name."""

    def failing(self, data):
        return {section for section, found in mst.check(data).items() if found}

    def patched(self, offset, value):
        data = bytearray(spec_example())
        data[offset : offset + len(value)] = value
        return bytes(data)

    def test_size_field(self):
        self.assertEqual(
            self.failing(self.patched(8, struct.pack(">I", 2177))), {"Header", "Round trip"}
        )

    def test_dirty_free_entry_row(self):
        self.assertEqual(self.failing(self.patched(190, b"\x01")), {"Entry table", "Round trip"})

    def test_dirty_free_support_row(self):
        self.assertEqual(self.failing(self.patched(220, b"\x01")), {"Support table", "Round trip"})

    def test_support_name_leftovers(self):
        data = build([(b"a.csv", b"x", 1, 1, None)], [(b"s.zz\0junk", 1)])
        self.assertEqual(self.failing(data), {"Support table", "Round trip"})

    def test_data_offset_field(self):
        failing = self.failing(self.patched(28, struct.pack(">I", 4096)))
        self.assertIn("Data region", failing)

    def test_dirty_gap_before_data(self):
        self.assertEqual(self.failing(self.patched(1000, b"\x01")), {"Data region", "Round trip"})

    def test_dirty_padding_between_entries(self):
        self.assertEqual(
            self.failing(self.patched(2120, b"\x01")), {"Entry data placement", "Round trip"}
        )

    def test_misplaced_entry(self):
        # Move beta.tga's location from 2144 to 2176 and grow the file to match.
        data = self.patched(144 + 20, struct.pack(">I", 2176)) + bytes(32)
        data = data[:8] + struct.pack(">I", len(data)) + data[12:]
        self.assertEqual(self.failing(data), {"Entry data placement", "Round trip"})

    def test_overlapping_entries(self):
        data = self.patched(144 + 20, struct.pack(">I", 2080))
        self.assertIn("Entry row", self.failing(data))

    def test_name_rules(self):
        for name in (b"Upper.csv", b"no_dot", b"two.dots.csv", b"long.type", b"a/b.csv"):
            data = build([(name, b"x", 1, 1, None)])
            self.assertEqual(self.failing(data), {"Entry names"}, name)

    def test_messages_never_hold_names(self):
        data = build(
            [(b"Secret-Name.csv", b"x", 1, 1, None), (b"secret-name.csv", b"y", 1, 1, None)]
        )
        problems = mst.check(data)
        text = repr(problems).lower()
        self.assertTrue(problems["Entry names"])
        self.assertNotIn("secret", text)
        self.assertNotIn("secret", repr(mst.check(data[:200])).lower())

    def test_cli(self):
        import contextlib
        import io
        import os
        import tempfile

        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "synthetic.mst")
            for data, code, last in ((spec_example(), 0, "PASSED"), (b"junk", 1, "FAILED")):
                with open(path, "wb") as f:
                    f.write(data)
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    self.assertEqual(mst.main(["check", path]), code)
                self.assertTrue(out.getvalue().splitlines()[-1].startswith(last))


if __name__ == "__main__":
    unittest.main()
