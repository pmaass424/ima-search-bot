import tempfile
import unittest
from pathlib import Path

from ima_research_bot.recency import path_report_timestamp, report_day
from ima_research_bot.state import StateStore
from ima_research_bot.sync import LocalToR2Sync


class FakeStorage:
    def __init__(self) -> None:
        self.uploads: dict[str, bytes] = {}

    def upload_file(self, path: Path, key: str, *, metadata=None) -> str:
        self.uploads[key] = path.read_bytes()
        return key


class R2ArchitectureTest(unittest.TestCase):
    def test_short_report_date_is_detected(self) -> None:
        path = Path("华鑫证券-电子行业周报-250818.pdf")

        self.assertEqual(report_day(path_report_timestamp(path)), "2025-08-18")

    def test_local_baseline_upload_records_storage_object_and_dedupes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            report = root / "fixed-base" / "20260626-test.pdf"
            report.parent.mkdir()
            report.write_bytes(b"pdf")
            state = StateStore(root / "state.sqlite3")
            storage = FakeStorage()

            first = LocalToR2Sync(report.parent, storage, state).run()
            second = LocalToR2Sync(report.parent, storage, state).run()
            stats = state.storage_stats()

        self.assertEqual(first.uploaded, 1)
        self.assertEqual(second.uploaded, 0)
        self.assertEqual(second.skipped, 1)
        self.assertEqual(stats["stored_objects"], 1)
        self.assertEqual(list(storage.uploads), ["baseline/2026-06-26/20260626-test.pdf"])


if __name__ == "__main__":
    unittest.main()
