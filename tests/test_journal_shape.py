import json
from pathlib import Path
import tempfile
import unittest

from eb_staged_rebuild.errors import NeedsIntervention
from eb_staged_rebuild.journal import load_journal


class JournalShapeTests(unittest.TestCase):
    def test_invalid_record_shapes_need_intervention(self):
        cases = (
            ([], "journal is not a JSON object"),
            (
                {"schema": 2, "modules_plan": "entry"},
                "journal modules_plan is not a list",
            ),
            (
                {"schema": 2, "modules_plan": [1]},
                "journal modules_plan entry is not an object",
            ),
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "journal.json"
            for value, message in cases:
                with self.subTest(value=value):
                    path.write_text(json.dumps(value))
                    with self.assertRaisesRegex(NeedsIntervention, message):
                        load_journal(path)


if __name__ == "__main__":
    unittest.main()
