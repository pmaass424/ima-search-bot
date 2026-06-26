import tempfile
import unittest
from pathlib import Path

from ima_research_bot.ima_human import (
    ImaHumanConfig,
    ImaHumanDownloader,
    _drop_opened_workspace_segment,
    duplicate_by_name_and_size,
    existing_download_index,
    extract_pdf_name,
    intermediate_folder_texts,
    latest_date_text,
    latest_month_text,
    normalize_match_text,
    normalize_filename,
    text_report_date,
    unique_destination,
)
from ima_research_bot.config import _parse_folder_paths


class _FakeBody:
    def __init__(self, text: str) -> None:
        self.text = text

    def inner_text(self, timeout: int = 0) -> str:
        return self.text


class _FakePage:
    def __init__(self, body_text: str) -> None:
        self.body_text = body_text

    def locator(self, selector: str) -> _FakeBody:
        assert selector == "body"
        return _FakeBody(self.body_text)


class ImaHumanHelpersTest(unittest.TestCase):
    def test_normalize_filename_keeps_chinese_and_removes_path_separators(self) -> None:
        self.assertEqual(
            normalize_filename("  20260621/华泰:研究报告.pdf  "),
            "20260621-华泰:研究报告.pdf",
        )

    def test_normalize_match_text_handles_spacing_and_ellipsis(self) -> None:
        self.assertEqual(
            normalize_match_text(" 六、高盛、花旗、瑞银、摩根等外资研... "),
            "六、高盛、花旗、瑞银、摩根等外资研",
        )

    def test_extract_pdf_name_from_multiline_item(self) -> None:
        self.assertEqual(
            extract_pdf_name("机构报告\n20260621-机器人行业周报.pdf\n12 MB"),
            "20260621-机器人行业周报.pdf",
        )

    def test_latest_date_text_picks_newest_visible_date(self) -> None:
        self.assertEqual(
            latest_date_text(["2026.06.20", "2026-06-22 reports", "2026年06月21日"]),
            "2026-06-22 reports",
        )

    def test_latest_date_text_supports_compact_dates_and_ignores_pdf_files(self) -> None:
        self.assertEqual(
            latest_date_text(["20260624", "20260625-report.pdf", "2026年06月23日"]),
            "20260624",
        )

    def test_latest_month_text_ignores_day_folders(self) -> None:
        self.assertEqual(
            latest_month_text(["2026年5月", "2026年6月📈", "2026-06-24"]),
            "2026年6月📈",
        )

    def test_text_report_date_picks_latest_date_inside_filename(self) -> None:
        self.assertEqual(str(text_report_date("foo-20260620-20260624-report.pdf")), "2026-06-24")

    def test_intermediate_folder_texts_filters_home_noise(self) -> None:
        self.assertEqual(
            intermediate_folder_texts(
                [
                    "首页",
                    "ima copilot",
                    "正在打开腾讯ima 若未安装，可点击下载客户端 下载腾讯",
                    "个人知识库",
                    "微信用户的知识库",
                    "共享知识库",
                    "ima知识库使用指南.docx",
                    "【爱分享】",
                    "【爱分享】的财经资讯",
                    "六、高盛、花旗、瑞银、摩根等外资研报",
                    "2026年6月",
                    "20260624-report.pdf",
                    "文档解读",
                ],
                exclude=["【爱分享】盈策系列"],
            ),
            ["六、高盛、花旗、瑞银、摩根等外资研报"],
        )

    def test_drop_opened_workspace_segment_avoids_clicking_workspace_twice(self) -> None:
        self.assertEqual(
            _drop_opened_workspace_segment(("【爱分享】盈策系列", "LATEST_SCOPE"), "【爱分享】"),
            ("LATEST_SCOPE",),
        )

    def test_parse_folder_paths_supports_multiple_visual_paths(self) -> None:
        self.assertEqual(
            _parse_folder_paths("A|B|LATEST_SCOPE;;A|C|LATEST_MONTH|LATEST_DAY"),
            (("A", "B", "LATEST_SCOPE"), ("A", "C", "LATEST_MONTH", "LATEST_DAY")),
        )

    def test_existing_index_ignores_hidden_staging_and_incomplete_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "ready.pdf").write_bytes(b"ok")
            (root / "partial.crdownload").write_bytes(b"no")
            (root / ".downloads").mkdir()
            (root / ".downloads" / "staged.pdf").write_bytes(b"no")

            index = existing_download_index(root)

        self.assertEqual(index, {"ready.pdf": 2})

    def test_duplicate_by_name_and_size_ignores_staging_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".downloads").mkdir()
            (root / ".downloads" / "same.pdf").write_bytes(b"abc")
            self.assertIsNone(duplicate_by_name_and_size("same.pdf", 3, root))
            (root / "same.pdf").write_bytes(b"abc")
            self.assertEqual(duplicate_by_name_and_size("same.pdf", 3, root), root / "same.pdf")

    def test_unique_destination_adds_suffix(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "report.pdf").write_bytes(b"first")
            self.assertEqual(unique_destination(root, "report.pdf"), root / "report-2.pdf")

    def test_login_detection_requires_auth_marker_without_content_signal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = ImaHumanConfig(
                url="https://ima.qq.com",
                profile_dir=Path(tmp) / "profile",
                download_dir=Path(tmp) / "inbox",
                max_downloads_per_cycle=3,
                poll_interval_minutes=10,
                headless=True,
            )
            downloader = ImaHumanDownloader(config)
            self.assertTrue(downloader._looks_like_login_or_challenge(_FakePage("请扫码登录")))
            self.assertTrue(downloader._looks_like_login_or_challenge(_FakePage("知识库\n登录以同步历史会话\n登录")))
            self.assertFalse(downloader._looks_like_login_or_challenge(_FakePage("存储空间\n【爱分享】\n首页")))
            self.assertFalse(downloader._looks_like_login_or_challenge(_FakePage("个人知识库\n登录以同步历史会话\n登录")))
            self.assertFalse(downloader._looks_like_login_or_challenge(_FakePage("知识库\nreport.pdf")))


if __name__ == "__main__":
    unittest.main()
