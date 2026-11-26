"""核验合成资料的可读性和事件顺序。"""

import tempfile
import unittest
from pathlib import Path

from sample_reader import load_inventory, load_timeline


class SourceTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(__file__).resolve().parents[1] / "samples"

    def test_inventory_and_event_series(self):
        self.assertEqual(set(load_inventory(self.root)), {'endpoints', 'resources', 'snapshot'})
        names = ["shift-log.jsonl", "partial-resume.jsonl"]
        primary, alternate = [list(load_timeline(self.root / name)) for name in names]
        self.assertEqual(len(primary), 12)
        self.assertEqual(len(alternate), 8)
        self.assertNotEqual(primary, alternate)

    def test_consecutive_observations_are_preserved(self):
        kinds = iter(record["kind"] for record in load_timeline(self.root / "shift-log.jsonl"))
        for expected in ('call', 'call', 'supersede', 'receipt', 'receipt', 'handoff', 'resume', 'receipt'):
            self.assertIn(expected, kinds)

    def test_backwards_arrival_points_to_input_line(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "invalid.jsonl"
            path.write_text('{"at": 5, "kind": "notice"}\n{"at": 2, "kind": "receipt"}\n',
                            encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "invalid.jsonl:2"):
                list(load_timeline(path))
