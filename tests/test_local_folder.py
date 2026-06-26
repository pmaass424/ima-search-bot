import os
import tempfile
import unittest
from pathlib import Path

from ima_research_bot.connectors.local_folder import LocalFolderConnector


class LocalFolderConnectorTest(unittest.TestCase):
    def setUp(self) -> None:
        self._old_env = {
            key: os.environ.get(key)
            for key in ("LOCAL_FILE_MIN_AGE_SECONDS", "LOCAL_LATEST_ONLY", "LOCAL_MAX_ITEMS")
        }
        os.environ["LOCAL_FILE_MIN_AGE_SECONDS"] = "0"
        os.environ["LOCAL_LATEST_ONLY"] = "1"
        os.environ["LOCAL_MAX_ITEMS"] = "200"

    def tearDown(self) -> None:
        for key, value in self._old_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def test_latest_only_keeps_newest_report_day(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "20260620-old.pdf").write_bytes(b"old")
            (root / "20260621-new-a.pdf").write_bytes(b"new-a")
            (root / "20260621-new-b.pdf").write_bytes(b"new-b")

            items = LocalFolderConnector(root).list_items()

        self.assertEqual(
            sorted(item.title for item in items),
            ["20260621-new-a.pdf", "20260621-new-b.pdf"],
        )


if __name__ == "__main__":
    unittest.main()
