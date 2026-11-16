"""Damaged input must be reported, never crash the tool or escape the output folder.

Each case is seeded, so a failure names a seed that reproduces it.
"""

import os
import random
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from kindle_acquire.extract import extract_image  # noqa: E402
from kindle_acquire.parsers import krds  # noqa: E402
from kindle_acquire.parsers.clippings import parse_clippings  # noqa: E402
from kindle_acquire.report import parse_acquisition  # noqa: E402
from kindle_acquire.verify import verify  # noqa: E402
import synthetic_kindle  # noqa: E402
from test_fat32 import BPS, CLUSTER, CLUSTERS  # noqa: E402

# Failures a damaged image may raise before any output exists.
EXPECTED = (ValueError, OSError)
# Set KEAT_FUZZ=10 (or more) for a longer run with more seeds.
SCALE = max(1, int(os.environ.get("KEAT_FUZZ", "1")))


class ImageFuzz(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.tmp = Path(cls._tmp.name)
        cls.good = synthetic_kindle.build_filesystem(cls.tmp).build()
        cls.data_start = len(cls.good) - CLUSTERS * CLUSTER

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def _run(self, data, label):
        image = self.tmp / "fuzz.dd"
        image.write_bytes(data)
        out = self.tmp / "out"
        if out.exists():
            shutil.rmtree(out)
        before = {p.name for p in self.tmp.iterdir()}
        try:
            log = extract_image(str(image), out, quiet=True)
        except EXPECTED:
            return None
        except Exception as exc:  # anything else is a defect
            self.fail("%s: %s: %s" % (label, type(exc).__name__, exc))
        # A run that returns must leave a consistent, verifiable folder.
        self.assertTrue((out / "acquisition_log.json").is_file(), label)
        result = verify(out)
        self.assertTrue(result["ok"], "%s: %s" % (label, result["problems"][:3]))
        after = {p.name for p in self.tmp.iterdir()}
        self.assertEqual(after - before, {"out"}, "%s: wrote outside the output folder" % label)
        try:
            parse_acquisition(out)
        except Exception as exc:
            self.fail("%s: parse failed with %s: %s" % (label, type(exc).__name__, exc))
        return log

    def test_undamaged_image_is_clean(self):
        log = self._run(self.good, "undamaged")
        self.assertEqual(log["errors"], [])

    def test_random_bytes_in_the_allocation_table(self):
        fat_start, fat_end = 32 * BPS, self.data_start
        for seed in range(20 * SCALE):
            rng = random.Random(seed)
            data = bytearray(self.good)
            for _ in range(rng.randint(1, 12)):
                data[rng.randrange(fat_start, fat_end)] = rng.randrange(256)
            self._run(bytes(data), "fat seed %d" % seed)

    def test_random_bytes_in_directories_and_data(self):
        for seed in range(35 * SCALE):
            rng = random.Random(1000 + seed)
            data = bytearray(self.good)
            for _ in range(rng.randint(1, 40)):
                # The first clusters hold the directories.
                span = 40 * CLUSTER if rng.random() < 0.7 else CLUSTERS * CLUSTER
                data[self.data_start + rng.randrange(span)] = rng.randrange(256)
            self._run(bytes(data), "data seed %d" % seed)

    def test_random_bytes_in_the_boot_sector(self):
        for seed in range(25 * SCALE):
            rng = random.Random(2000 + seed)
            data = bytearray(self.good)
            for _ in range(rng.randint(1, 4)):
                data[rng.randrange(11, 90)] = rng.randrange(256)
            self._run(bytes(data), "boot seed %d" % seed)

    def test_truncation_at_any_point(self):
        rng = random.Random(3000)
        for _ in range(15 * SCALE):
            cut = rng.randrange(0, len(self.good))
            self._run(self.good[:cut], "truncated at %d" % cut)

    def test_blocks_of_one_value(self):
        for seed in range(10 * SCALE):
            rng = random.Random(4000 + seed)
            data = bytearray(self.good)
            start = rng.randrange(0, len(data) - 4096)
            length = rng.randrange(1, 4096)
            data[start:start + length] = bytes([rng.choice((0x00, 0xFF, 0xE5, 0x20))]) * length
            self._run(bytes(data), "block seed %d" % seed)


class ParserFuzz(unittest.TestCase):
    def test_sidecar_decoder_only_raises_its_own_error(self):
        good = synthetic_kindle.sidecar(
            [("highlight", "A:1", "A:2", 1794715200533, ""),
             ("note", "A:3", "A:3", 1794715200533, "note text")], "A:9", 1794715200533)
        for seed in range(600):
            rng = random.Random(seed)
            data = bytearray(good)
            choice = rng.random()
            if choice < 0.4:
                for _ in range(rng.randint(1, 6)):
                    data[rng.randrange(8, len(data))] = rng.randrange(256)
            elif choice < 0.7:
                data = data[:rng.randrange(8, len(data))]
            else:
                at = rng.randrange(8, len(data))
                data[at:at] = bytes(rng.randrange(256) for _ in range(rng.randint(1, 20)))
            try:
                decoded = krds.decode(bytes(data))
                krds.annotations(decoded)
                krds.positions(decoded)
            except krds.KrdsError:
                pass
            except Exception as exc:
                self.fail("seed %d: %s: %s" % (seed, type(exc).__name__, exc))

    def test_clippings_parser_accepts_any_text(self):
        good = synthetic_kindle.CLIPPINGS
        for seed in range(300):
            rng = random.Random(seed)
            chars = list(good)
            for _ in range(rng.randint(1, 30)):
                at = rng.randrange(len(chars))
                chars[at] = rng.choice(["\n", "\r", "=", "|", "(", ")", "-", "\x00", "﻿", " ", "9"])
            if rng.random() < 0.3:
                chars = chars[:rng.randrange(len(chars))]
            try:
                entries = parse_clippings("".join(chars))
            except Exception as exc:
                self.fail("seed %d: %s: %s" % (seed, type(exc).__name__, exc))
            self.assertIsInstance(entries, list)


if __name__ == "__main__":
    unittest.main()
