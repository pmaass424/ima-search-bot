import logging
import os
import re
import shutil
import time
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from .config import Settings
from .notifiers import TelegramNotifier

logger = logging.getLogger(__name__)


INCOMPLETE_SUFFIXES = {".crdownload", ".part", ".download", ".tmp", ".qkdownloading"}
COMPACT_DATE_PATTERN = re.compile(r"(?<!\d)(20\d{2})(0[1-9]|1[0-2])([0-2]\d|3[01])(?!\d)")
SEPARATED_DATE_PATTERN = re.compile(
    r"(20\d{2})\s*(?:[./-]|\u5e74)\s*"
    r"(0?[1-9]|1[0-2])\s*(?:[./-]|\u6708)\s*"
    r"([12]\d|3[01]|0?[1-9])\s*(?:\u65e5)?"
)
MONTH_PATTERN = re.compile(
    r"(20\d{2})\s*(?:[./-]|\u5e74)\s*"
    r"(0?[1-9]|1[0-2])\s*(?:\u6708)?"
)
PDF_PATTERN = re.compile(r"\.pdf(?:\s|$)", re.IGNORECASE)
LOGIN_MARKERS = (
    "登录",
    "登陆",
    "扫码",
    "二维码",
    "验证码",
    "安全验证",
    "captcha",
    "verify",
    "sign in",
    "log in",
)
STRONG_LOGIN_MARKERS = (
    "登录以同步历史会话",
    "登录后",
    "请先登录",
)
DESKTOP_LABELS = ("打开电脑版", "Desktop", "Open desktop")
NAV_LABELS = (
    "存储空间",
    "存儲空間",
    "共享空间",
    "共享空間",
    "知识库",
    "資料庫",
    "资料库",
    "Knowledge",
    "Documents",
    "文件",
)
AUTH_CONTENT_LABELS = (
    "存储空间",
    "存儲空間",
    "个人知识库",
    "共享知识库",
    "订阅知识库",
    "微信用户的知识库",
    "内容(",
    "content(",
)
DOWNLOAD_LABELS = (
    "下载",
    "下載",
    "Download",
    "导出",
    "Export",
    "保存",
    "Save",
)
UI_NOISE_TEXTS = {
    "首页",
    "主页",
    "copilot",
    "ima copilot",
    "个人知识库",
    "微信用户的知识库",
    "共享知识库",
    "订阅知识库",
    "知识库",
    "存储空间",
    "共享空间",
    "正在打开腾讯ima",
    "若未安装",
    "点击下载客户端",
    "下载腾讯",
    "正在加载",
    "已使用",
    "免费领",
    "免费额度",
    "使用指南",
    "订阅知识库",
    "基于知识库问答",
    "对话模式",
    "ds 快速",
    "录音纪要",
    "文档解读",
    "智能写作",
    "图像生成",
    "快捷访问",
    "新版",
    "api key 获取",
    "打开电脑版",
}
LATEST_MONTH_TOKEN = "LATEST_MONTH"
LATEST_DAY_TOKEN = "LATEST_DAY"
LATEST_DATE_TOKEN = "LATEST_DATE"
LATEST_SCOPE_TOKENS = {"LATEST_SCOPE", "LATEST_REPORT_SCOPE", "LATEST_AUTO"}


class ImaHumanError(RuntimeError):
    pass


class ImaHumanLoginRequired(ImaHumanError):
    pass


class ImaHumanUiNotFound(ImaHumanError):
    pass


@dataclass(frozen=True)
class ImaHumanConfig:
    url: str
    profile_dir: Path
    download_dir: Path
    max_downloads_per_cycle: int
    poll_interval_minutes: int
    headless: bool
    folder_paths: tuple[tuple[str, ...], ...] = ()
    cdp_url: str = ""

    @property
    def staging_dir(self) -> Path:
        return self.download_dir / ".downloads"

    @classmethod
    def from_settings(cls, settings: Settings) -> "ImaHumanConfig":
        return cls(
            url=settings.ima_human_url,
            cdp_url=settings.ima_human_cdp_url,
            profile_dir=settings.ima_human_profile_dir,
            download_dir=settings.ima_human_download_dir,
            max_downloads_per_cycle=settings.ima_human_max_downloads_per_cycle,
            poll_interval_minutes=settings.ima_human_poll_interval_minutes,
            headless=settings.ima_human_headless,
            folder_paths=settings.ima_human_folder_paths,
        )


@dataclass
class ImaHumanCycleResult:
    downloaded: int = 0
    skipped_existing: int = 0
    attempted: int = 0
    status: str = "ok"
    message: str = ""


class ImaHumanDownloader:
    def __init__(self, config: ImaHumanConfig) -> None:
        self.config = config

    def login(self) -> None:
        self._prepare_dirs()
        with _playwright() as p:
            context, close_context = self._open_browser_context(p, headless=False)
            try:
                page = _initial_page(context, prefer_existing=bool(self.config.cdp_url))
                page = self._open_home(page, context)
                logger.info("login browser is open; complete Tencent/QQ/WeChat login, then stop with Ctrl+C")
                login_confirmed = False
                last_wait_log = 0.0
                try:
                    while True:
                        time.sleep(3)
                        if _context_is_closed(context):
                            logger.info("login browser was closed; profile kept at %s", self.config.profile_dir)
                            return
                        page = _best_context_page(context, fallback=page)
                        if self._looks_like_login_or_challenge(page):
                            now = time.monotonic()
                            if now - last_wait_log > 30:
                                logger.info("waiting for IMA login/captcha to complete")
                                last_wait_log = now
                            continue
                        if not login_confirmed:
                            self._open_knowledge_area(page)
                        if not login_confirmed and self._has_authenticated_content_signal(page):
                            login_confirmed = True
                            self._remember_authenticated_url(page)
                            logger.info(
                                "IMA login appears complete; keep the knowledge base visible, then stop with Ctrl+C"
                            )
                        elif not login_confirmed:
                            now = time.monotonic()
                            if now - last_wait_log > 30:
                                logger.info("waiting for authenticated IMA knowledge base to appear")
                                last_wait_log = now
                except KeyboardInterrupt:
                    logger.info("login browser stopped by user; profile kept at %s", self.config.profile_dir)
            finally:
                try:
                    close_context()
                except Exception as exc:
                    logger.info("browser was already closed while stopping login: %s", exc)

    def run_cycle(self) -> ImaHumanCycleResult:
        self._prepare_dirs()
        result = ImaHumanCycleResult()
        with _playwright() as p:
            context, close_context = self._open_browser_context(p, headless=self.config.headless)
            try:
                page = _initial_page(context, prefer_existing=bool(self.config.cdp_url))
                paths = self.config.folder_paths or ((),)
                for folder_path in paths:
                    if result.downloaded >= max(0, self.config.max_downloads_per_cycle):
                        break
                    logger.info("opening IMA home for download cycle")
                    page = self._open_home(page, context)
                    opened_workspace = self._open_knowledge_area(page)
                    if not self._has_authenticated_content_signal(page) or self._looks_like_login_or_challenge(page):
                        self._write_debug_snapshot(page, "login-required")
                        raise ImaHumanLoginRequired(
                            "IMA login/captcha/security challenge detected; run ima-human-login manually. "
                            f"Visible page text: {_page_text_excerpt(page)}"
                        )
                    try:
                        if folder_path:
                            effective_folder_path = _drop_opened_workspace_segment(folder_path, opened_workspace)
                            try:
                                self._open_folder_path(page, effective_folder_path)
                            except ImaHumanUiNotFound:
                                page = self._open_home(page, context)
                                opened_workspace = self._open_knowledge_area(page)
                                effective_folder_path = _drop_opened_workspace_segment(folder_path, opened_workspace)
                                self._open_folder_path(page, effective_folder_path)
                        else:
                            self._open_knowledge_area(page)
                            self._open_latest_date_folder(page)
                        remaining = max(0, self.config.max_downloads_per_cycle - result.downloaded)
                        path_result = self._download_visible_pdfs(page, max_downloads=remaining)
                        result.downloaded += path_result.downloaded
                        result.skipped_existing += path_result.skipped_existing
                        result.attempted += path_result.attempted
                    except ImaHumanUiNotFound as exc:
                        logger.warning("IMA path skipped: %s", exc)
                        result.message = (result.message + "\n" + str(exc)).strip()
                        continue
                if result.message and result.downloaded == 0 and result.skipped_existing == 0 and result.attempted == 0:
                    raise ImaHumanUiNotFound(result.message)
                logger.info(
                    "IMA human cycle finished: downloaded=%s skipped_existing=%s attempted=%s",
                    result.downloaded,
                    result.skipped_existing,
                    result.attempted,
                )
                return result
            finally:
                close_context()

    def _open_browser_context(self, playwright: Any, headless: bool) -> tuple[Any, Any]:
        if self.config.cdp_url:
            logger.info("connecting to existing IMA Chromium app via CDP: %s", self.config.cdp_url)
            browser = playwright.chromium.connect_over_cdp(self.config.cdp_url)
            contexts = list(getattr(browser, "contexts", []) or [])
            if not contexts:
                raise ImaHumanUiNotFound(
                    "connected to IMA CDP, but no browser context was exposed"
                )
            return contexts[0], lambda: None
        context = playwright.chromium.launch_persistent_context(
            user_data_dir=str(self.config.profile_dir),
            headless=headless,
            accept_downloads=True,
            downloads_path=str(self.config.staging_dir),
            viewport={"width": 1440, "height": 1000},
        )
        return context, context.close

    def _prepare_dirs(self) -> None:
        self.config.profile_dir.mkdir(parents=True, exist_ok=True)
        self.config.download_dir.mkdir(parents=True, exist_ok=True)
        self.config.staging_dir.mkdir(parents=True, exist_ok=True)

    def _open_home(self, page: Any, context: Optional[Any] = None) -> Any:
        if self.config.cdp_url and _is_ima_app_page(page):
            logger.info("using existing IMA desktop app page: %s", getattr(page, "url", ""))
            _dismiss_blocking_popups(page)
            return _best_context_page(context, fallback=page) if context is not None else page
        target_url = self._remembered_authenticated_url() or self.config.url
        try:
            page.goto(target_url, wait_until="commit", timeout=60_000)
        except Exception as exc:
            if not _is_playwright_timeout(exc):
                raise
            logger.warning("IMA page navigation timed out after initial load; continuing with current page state")
        try:
            page.wait_for_load_state("networkidle", timeout=15_000)
        except Exception as exc:
            if not _is_playwright_timeout(exc):
                raise
        _dismiss_blocking_popups(page)
        page = self._open_desktop_if_available(page, context)
        return _best_context_page(context, fallback=page) if context is not None else page

    def _remembered_authenticated_url(self) -> str:
        path = self.config.profile_dir / ".ima-human-authenticated-url"
        try:
            value = path.read_text(encoding="utf-8").strip()
        except OSError:
            return ""
        if value.startswith(("http://", "https://")):
            return value
        return ""

    def _remember_authenticated_url(self, page: Any) -> None:
        try:
            url = str(page.url or "")
        except Exception:
            return
        if not url.startswith(("http://", "https://")):
            return
        try:
            (self.config.profile_dir / ".ima-human-authenticated-url").write_text(url, encoding="utf-8")
            logger.info("saved authenticated IMA URL for future download cycles: %s", url)
        except OSError as exc:
            logger.info("could not save authenticated IMA URL: %s", exc)

    def _open_desktop_if_available(self, page: Any, context: Optional[Any] = None) -> Any:
        text = _body_text(page)
        if any(label in text for label in DESKTOP_LABELS):
            for label in DESKTOP_LABELS:
                page_count = len(context.pages) if context is not None else 0
                if _click_text_if_visible(page, label, timeout=3_000):
                    logger.info("opened IMA desktop interface via %s", label)
                    if context is not None:
                        _settle_newest_page(context, previous_page_count=page_count)
                        return _best_context_page(context, fallback=page)
                    return page
        return page

    def _looks_like_login_or_challenge(self, page: Any) -> bool:
        text = _body_text(page).lower()
        if not text:
            return False
        marker_count = sum(1 for marker in LOGIN_MARKERS if marker.lower() in text)
        has_content_signal = self._has_authenticated_content_signal(page, text=text)
        if has_content_signal:
            return False
        if any(marker.lower() in text for marker in STRONG_LOGIN_MARKERS):
            return True
        return marker_count > 0 and not has_content_signal

    def _has_authenticated_content_signal(self, page: Any, text: Optional[str] = None) -> bool:
        body = (text if text is not None else _body_text(page)).lower()
        configured_segments = {
            segment.strip().lower()
            for path in self.config.folder_paths
            for segment in path
            if segment.strip()
            and segment.strip().upper() not in {LATEST_MONTH_TOKEN, LATEST_DAY_TOKEN, LATEST_DATE_TOKEN, *LATEST_SCOPE_TOKENS}
        }
        return (
            any(label.lower() in body for label in AUTH_CONTENT_LABELS)
            or ".pdf" in body
            or any(segment in body for segment in configured_segments)
        )

    def _open_knowledge_area(self, page: Any) -> str:
        for label in self._configured_workspace_labels():
            if _click_text_if_visible(page, label, timeout=2_000):
                logger.info("opened configured IMA workspace via %s", label)
                _settle(page)
                return label
        for label in NAV_LABELS:
            if _click_text_if_visible(page, label, timeout=1_500):
                logger.info("opened IMA knowledge/storage area via %s", label)
                _settle(page)
                return ""
        logger.info("knowledge navigation label not found; continuing from current IMA page")
        return ""

    def _configured_workspace_labels(self) -> list[str]:
        labels: list[str] = []
        for folder_path in self.config.folder_paths:
            for segment in folder_path[:1]:
                segment = segment.strip()
                if segment and segment.upper() not in {LATEST_MONTH_TOKEN, LATEST_DAY_TOKEN, LATEST_DATE_TOKEN, *LATEST_SCOPE_TOKENS}:
                    labels.append(segment)
        return labels

    def _open_latest_date_folder(self, page: Any) -> None:
        texts = _visible_texts(page, limit=250)
        latest = latest_date_text(texts)
        if not latest:
            logger.info("no visible date folder found; continuing with current visible list")
            return
        logger.info("opening latest visible IMA folder/date: %s", latest)
        if not _click_text_if_visible(page, latest, timeout=5_000):
            raise ImaHumanUiNotFound(f"found latest date text but could not click it: {latest}")
        _settle(page)

    def _open_folder_path(self, page: Any, folder_path: tuple[str, ...]) -> None:
        for segment in folder_path:
            if segment.strip().upper() in LATEST_SCOPE_TOKENS:
                self._open_latest_report_scope(page)
                continue
            target = self._resolve_path_segment(page, segment)
            logger.info("opening IMA path segment %s -> %s", segment, target)
            if not target:
                raise ImaHumanUiNotFound(f"could not resolve path segment: {segment}")
            if not _click_text_if_visible(page, target, timeout=6_000):
                visible = "; ".join(_visible_texts(page, limit=40)[:20])
                raise ImaHumanUiNotFound(f"could not click path segment: {target}. Visible texts: {visible}")
            _settle(page)

    def _resolve_path_segment(self, page: Any, segment: str) -> str:
        normalized = segment.strip().upper()
        texts = _visible_texts(page, limit=350)
        if normalized == LATEST_MONTH_TOKEN:
            return latest_month_text(texts)
        if normalized in {LATEST_DAY_TOKEN, LATEST_DATE_TOKEN}:
            return latest_date_text(texts)
        return segment.strip()

    def _open_latest_report_scope(self, page: Any) -> None:
        max_depth = int(os.getenv("IMA_HUMAN_LATEST_SCOPE_DEPTH", "6"))
        opened: set[str] = set()
        for _ in range(max(1, max_depth)):
            if _has_visible_pdf(page):
                logger.info("latest scope reached visible PDFs")
                return
            texts = _visible_texts(page, limit=350)
            target = latest_date_text(texts) or latest_month_text(texts)
            target_kind = "date/month"
            if not target:
                candidates = intermediate_folder_texts(
                    texts,
                    exclude=[*opened, *self._configured_workspace_labels()],
                )
                if not candidates:
                    visible = "; ".join(texts[:20])
                    logger.info("latest scope found no date/month or intermediate folder. Visible texts: %s", visible)
                    return
                target = candidates[0]
                target_kind = "intermediate folder"
            normalized_target = normalize_match_text(target)
            if normalized_target in opened:
                logger.info("latest scope stopped after reaching already-open scope: %s", target)
                return
            logger.info("latest scope opening visible %s: %s", target_kind, target)
            if not _click_text_if_visible(page, target, timeout=6_000):
                if _has_visible_pdf(page):
                    logger.info("latest scope could not click %s but PDFs are visible; staying here", target)
                    return
                raise ImaHumanUiNotFound(f"could not click latest scope segment: {target}")
            opened.add(normalized_target)
            _settle(page)
            if _has_visible_pdf(page) and target == latest_date_text(_visible_texts(page, limit=350)):
                logger.info("latest scope reached newest day with visible PDFs: %s", target)
                return

    def _download_visible_pdfs(self, page: Any, max_downloads: Optional[int] = None) -> ImaHumanCycleResult:
        result = ImaHumanCycleResult()
        existing = existing_download_index(self.config.download_dir)
        limit = self.config.max_downloads_per_cycle if max_downloads is None else max_downloads
        pdf_locator = page.get_by_text(PDF_PATTERN)
        try:
            count = min(pdf_locator.count(), 80)
        except Exception as exc:
            raise ImaHumanUiNotFound("could not inspect PDF items on IMA page") from exc
        if count <= 0:
            visible = "; ".join(_visible_texts(page, limit=40)[:20])
            raise ImaHumanUiNotFound(f"no visible PDF items found on IMA page. Visible texts: {visible}")

        candidates = []
        for idx in range(count):
            item = pdf_locator.nth(idx)
            try:
                raw_text = item.inner_text(timeout=1_000).strip()
            except Exception:
                raw_text = ""
            expected_name = extract_pdf_name(raw_text) or f"ima-download-{idx + 1}.pdf"
            candidates.append((idx, item, raw_text, expected_name, text_report_date(expected_name) or text_report_date(raw_text)))

        if os.getenv("IMA_HUMAN_LATEST_VISIBLE_FILES_ONLY", "1") != "0":
            dated = [candidate for candidate in candidates if candidate[4] is not None]
            if dated:
                latest = max(candidate[4] for candidate in dated if candidate[4] is not None)
                before = len(candidates)
                candidates = [candidate for candidate in candidates if candidate[4] == latest]
                logger.info(
                    "kept %s/%s visible PDFs for latest report day %s",
                    len(candidates),
                    before,
                    latest.isoformat() if latest else "",
                )

        for _, item, _, expected_name, _ in candidates:
            if result.downloaded >= max(0, limit):
                break
            normalized = normalize_filename(expected_name)
            if normalized.lower() in existing:
                result.skipped_existing += 1
                logger.info("skipping existing IMA PDF: %s", normalized)
                continue
            result.attempted += 1
            download = self._trigger_download(page, item)
            if not download:
                logger.info("no download was emitted for visible PDF item: %s", expected_name)
                continue
            final_path = self._save_download(download, fallback_name=normalized)
            existing[normalize_filename(final_path.name).lower()] = final_path.stat().st_size
            result.downloaded += 1
            logger.info("saved IMA PDF: %s", final_path)
        return result

    def _trigger_download(self, page: Any, item: Any) -> Optional[Any]:
        try:
            with page.expect_download(timeout=12_000) as download_info:
                item.click(timeout=5_000)
            return download_info.value
        except Exception as exc:
            if not _is_playwright_timeout(exc):
                raise

        _settle(page)
        for label in DOWNLOAD_LABELS:
            try:
                with page.expect_download(timeout=12_000) as download_info:
                    if not _click_text_if_visible(page, label, timeout=2_000):
                        continue
                return download_info.value
            except Exception as exc:
                if not _is_playwright_timeout(exc):
                    raise
        return None

    def _save_download(self, download: Any, fallback_name: str) -> Path:
        suggested = normalize_filename(getattr(download, "suggested_filename", "") or fallback_name)
        if not suggested.lower().endswith(".pdf"):
            suggested = f"{Path(suggested).stem or Path(fallback_name).stem}.pdf"
        staged = self.config.staging_dir / suggested
        download.save_as(str(staged))
        duplicate = duplicate_by_name_and_size(suggested, staged.stat().st_size, self.config.download_dir)
        if duplicate:
            staged.unlink(missing_ok=True)
            logger.info("download matched existing file by name and size: %s", duplicate)
            return duplicate
        final_path = unique_destination(self.config.download_dir, suggested)
        shutil.move(str(staged), final_path)
        return final_path

    def _write_debug_snapshot(self, page: Any, reason: str) -> None:
        debug_dir = self.config.download_dir / ".debug"
        debug_dir.mkdir(parents=True, exist_ok=True)
        timestamp = time.strftime("%Y%m%d-%H%M%S")
        stem = f"ima-{reason}-{timestamp}"
        try:
            (debug_dir / f"{stem}.txt").write_text(_page_text_excerpt(page, limit=4000), encoding="utf-8")
        except Exception as exc:
            logger.info("could not write IMA debug text snapshot: %s", exc)
        try:
            page.screenshot(path=str(debug_dir / f"{stem}.png"), full_page=True, timeout=5_000)
        except Exception as exc:
            logger.info("could not write IMA debug screenshot: %s", exc)


def serve_human_downloader(settings: Settings) -> None:
    config = ImaHumanConfig.from_settings(settings)
    downloader = ImaHumanDownloader(config)
    notifier = TelegramNotifier(settings.telegram_bot_token, settings.telegram_chat_id)
    logger.info(
        "starting IMA human downloader; url=%s download_dir=%s interval=%s max_downloads=%s headless=%s",
        config.url,
        config.download_dir,
        config.poll_interval_minutes,
        config.max_downloads_per_cycle,
        config.headless,
    )
    while True:
        try:
            result = downloader.run_cycle()
            logger.info("IMA human downloader status: %s", result)
        except ImaHumanLoginRequired as exc:
            _notify(notifier, f"[IMA human downloader]\n\nLogin/manual verification required:\n{exc}")
            logger.warning("%s", exc)
        except ImaHumanUiNotFound as exc:
            _notify(notifier, f"[IMA human downloader]\n\nIMA UI not found or changed:\n{exc}")
            logger.warning("%s", exc)
        except Exception as exc:
            logger.exception("IMA human downloader cycle failed")
            _notify(notifier, f"[IMA human downloader]\n\nCycle failed: {type(exc).__name__}: {exc}")
        time.sleep(max(1, config.poll_interval_minutes) * 60)


def normalize_filename(value: str) -> str:
    value = unicodedata.normalize("NFC", value or "")
    value = value.replace("\x00", "")
    value = re.sub(r"[\\/]+", "-", value)
    value = re.sub(r"\s+", " ", value).strip(" .")
    return value[:180] or "download.pdf"


def normalize_match_text(value: str) -> str:
    value = unicodedata.normalize("NFKC", value or "")
    value = re.sub(r"\s+", "", value)
    value = value.replace("…", "").replace("...", "")
    return value.strip().lower()


def _drop_opened_workspace_segment(folder_path: tuple[str, ...], opened_workspace: str) -> tuple[str, ...]:
    if not folder_path or not opened_workspace:
        return folder_path
    first = normalize_match_text(folder_path[0])
    opened = normalize_match_text(opened_workspace)
    if not first or not opened:
        return folder_path
    if first == opened or first in opened or opened in first:
        return folder_path[1:]
    return folder_path


def extract_pdf_name(text: str) -> str:
    if not text:
        return ""
    for line in text.splitlines():
        if PDF_PATTERN.search(line):
            before, _, _ = line.partition(".pdf")
            return f"{before.strip()}.pdf"
    match = re.search(r"([^\n\r]+?\.pdf)", text, flags=re.IGNORECASE)
    return match.group(1).strip() if match else ""


def text_report_date(text: str) -> Optional[date]:
    best: Optional[date] = None
    for match in COMPACT_DATE_PATTERN.finditer(text):
        best = _max_report_date(best, int(match.group(1)), int(match.group(2)), int(match.group(3)))
    for match in SEPARATED_DATE_PATTERN.finditer(text):
        best = _max_report_date(best, int(match.group(1)), int(match.group(2)), int(match.group(3)))
    return best


def _max_report_date(current: Optional[date], year: int, month: int, day: int) -> Optional[date]:
    try:
        candidate = date(year, month, day)
    except ValueError:
        return current
    # IMA is China-facing; tolerate one-day timezone skew, but ignore obvious future dates.
    china_today = datetime.now(timezone(timedelta(hours=8))).date()
    if candidate > china_today + timedelta(days=1):
        return current
    if current is None or candidate > current:
        return candidate
    return current


def latest_date_text(texts: list[str]) -> str:
    candidates: list[tuple[date, str]] = []
    for text in texts:
        text = text.strip()
        if not text or PDF_PATTERN.search(text):
            continue
        parsed = text_report_date(text)
        if not parsed:
            continue
        candidates.append((parsed, text))
    if not candidates:
        return ""
    candidates.sort(key=lambda item: item[0], reverse=True)
    return candidates[0][1]


def latest_month_text(texts: list[str]) -> str:
    candidates: list[tuple[tuple[int, int], str]] = []
    for text in texts:
        text = text.strip()
        if not text or PDF_PATTERN.search(text) or text_report_date(text):
            continue
        match = MONTH_PATTERN.search(text)
        if not match:
            continue
        year, month = (int(part) for part in match.groups())
        candidates.append(((year, month), text.strip()))
    if not candidates:
        return ""
    candidates.sort(key=lambda item: item[0], reverse=True)
    return candidates[0][1]


def intermediate_folder_texts(texts: list[str], exclude: Optional[list[str]] = None) -> list[str]:
    excluded = {normalize_match_text(text) for text in (exclude or []) if text}
    candidates: list[str] = []
    seen: set[str] = set()
    for text in texts:
        label = re.sub(r"\s+", " ", text or "").strip()
        normalized = normalize_match_text(label)
        if not _looks_like_intermediate_folder(label, normalized, excluded):
            continue
        if normalized in seen:
            continue
        seen.add(normalized)
        candidates.append(label)
    return candidates


def _looks_like_intermediate_folder(label: str, normalized: str, excluded: set[str]) -> bool:
    if not label or not normalized:
        return False
    if normalized in excluded or any(
        item and (item in normalized or normalized in item) and min(len(item), len(normalized)) >= 4
        for item in excluded
    ):
        return False
    if _looks_like_other_shared_workspace(label, excluded):
        return False
    if PDF_PATTERN.search(label) or text_report_date(label) or MONTH_PATTERN.search(label):
        return False
    if len(normalized) < 2 or len(label) > 120:
        return False
    if any(normalize_match_text(noise) in normalized for noise in UI_NOISE_TEXTS):
        return False
    if "知识库" in label:
        return False
    if label.endswith((".docx", ".doc", ".xlsx", ".pptx", ".txt")):
        return False
    if any(normalize_match_text(nav) == normalized for nav in NAV_LABELS):
        return False
    if any(normalize_match_text(download) == normalized for download in DOWNLOAD_LABELS):
        return False
    if re.fullmatch(r"[\d\s:：./_-]+", label):
        return False
    return True


def _looks_like_other_shared_workspace(label: str, excluded: set[str]) -> bool:
    label_prefix = _bracket_prefix(label)
    if not label_prefix:
        return False
    normalized_label = normalize_match_text(label)
    for item in excluded:
        if not item:
            continue
        item_prefix = _bracket_prefix(item)
        if item_prefix and item_prefix == label_prefix and item != normalized_label:
            return True
    return False


def _bracket_prefix(value: str) -> str:
    match = re.match(r"^\s*(【[^】]+】)", value or "")
    return normalize_match_text(match.group(1)) if match else ""


def _has_visible_pdf(page: Any) -> bool:
    try:
        return page.get_by_text(PDF_PATTERN).count() > 0
    except Exception:
        return ".pdf" in _body_text(page).lower()


def existing_download_index(download_dir: Path) -> dict[str, int]:
    index: dict[str, int] = {}
    for path in download_dir.rglob("*"):
        if not path.is_file():
            continue
        if path.name.startswith(".") or any(part.startswith(".") for part in path.relative_to(download_dir).parts):
            continue
        if path.suffix.lower() in INCOMPLETE_SUFFIXES:
            continue
        index[normalize_filename(path.name).lower()] = path.stat().st_size
    return index


def duplicate_by_name_and_size(filename: str, size: int, download_dir: Path) -> Optional[Path]:
    normalized = normalize_filename(filename).lower()
    for path in download_dir.rglob("*"):
        if not path.is_file() or path.name.startswith("."):
            continue
        if any(part.startswith(".") for part in path.relative_to(download_dir).parts):
            continue
        if normalize_filename(path.name).lower() == normalized and path.stat().st_size == size:
            return path
    return None


def unique_destination(download_dir: Path, filename: str) -> Path:
    filename = normalize_filename(filename)
    candidate = download_dir / filename
    if not candidate.exists():
        return candidate
    stem = candidate.stem
    suffix = candidate.suffix
    for idx in range(2, 10_000):
        candidate = download_dir / f"{stem}-{idx}{suffix}"
        if not candidate.exists():
            return candidate
    raise RuntimeError(f"could not allocate destination for {filename}")


def _playwright() -> Any:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise RuntimeError(
            "Playwright is not installed. Run: pip install -e . && python -m playwright install chromium --with-deps"
        ) from exc
    return sync_playwright()


def _initial_page(context: Any, prefer_existing: bool = False) -> Any:
    pages = list(getattr(context, "pages", []) or [])
    if prefer_existing and pages:
        return _best_context_page(context, fallback=pages[-1])
    return _fresh_page(context)


def _fresh_page(context: Any) -> Any:
    return context.new_page()


def _best_context_page(context: Any, fallback: Any) -> Any:
    pages = list(getattr(context, "pages", []) or [])
    for page in reversed(pages):
        if _has_useful_ima_text(page):
            return page
    return pages[-1] if pages else fallback


def _settle_newest_page(context: Any, previous_page_count: int) -> None:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        pages = list(getattr(context, "pages", []) or [])
        if len(pages) > previous_page_count:
            _settle(pages[-1])
            return
        time.sleep(0.2)


def _has_useful_ima_text(page: Any) -> bool:
    text = _body_text(page).lower()
    return any(label.lower() in text for label in AUTH_CONTENT_LABELS) or ".pdf" in text


def _is_ima_app_page(page: Any) -> bool:
    try:
        url = str(page.url or "")
    except Exception:
        url = ""
    if url.startswith(("chrome://allknowledge", "chrome-extension://")):
        return True
    return _has_useful_ima_text(page)


def _body_text(page: Any) -> str:
    try:
        return page.locator("body").inner_text(timeout=3_000)
    except Exception:
        return ""


def _page_text_excerpt(page: Any, limit: int = 500) -> str:
    text = re.sub(r"\s+", " ", _body_text(page)).strip()
    if len(text) <= limit:
        return text or "<empty>"
    return f"{text[:limit].rstrip()}..."


def _context_is_closed(context: Any) -> bool:
    try:
        return len(context.pages) == 0
    except Exception:
        return True


def _visible_texts(page: Any, limit: int) -> list[str]:
    script = """
    (limit) => {
      const normalize = (value) => (value || '').replace(/\\s+/g, ' ').trim();
      const nodes = Array.from(document.querySelectorAll('a,button,[role=button],[role=treeitem],[role=listitem],[tabindex],div,span,p'));
      const out = [];
      const seen = new Set();
      for (const node of nodes) {
        const style = window.getComputedStyle(node);
        const rect = node.getBoundingClientRect();
        if (style.visibility === 'hidden' || style.display === 'none' || rect.width === 0 || rect.height === 0) continue;
        const text = normalize(node.innerText || node.textContent || '');
        if (!text || text.length > 180) continue;
        const childTexts = Array.from(node.children || [])
          .map((child) => normalize(child.innerText || child.textContent || ''))
          .filter(Boolean);
        const hasSameTextChild = childTexts.some((childText) => childText === text);
        const isInteractive = node.matches('a,button,[role=button],[role=treeitem],[role=listitem],[tabindex]') || style.cursor === 'pointer' || !!node.onclick;
        if (hasSameTextChild && !isInteractive) continue;
        if (seen.has(text)) continue;
        seen.add(text);
        out.push(text);
        if (out.length >= limit) break;
      }
      return out;
    }
    """
    try:
        return list(page.evaluate(script, limit))
    except Exception:
        return []


def _click_text_if_visible(page: Any, text: str, timeout: int) -> bool:
    if _click_visible_text_with_js(page, text):
        return True
    locator = page.get_by_text(text, exact=False).first
    try:
        if locator.count() <= 0:
            return False
        locator.click(timeout=timeout)
        return True
    except Exception as exc:
        return False


def _click_visible_text_with_js(page: Any, text: str) -> bool:
    script = """
    (needle) => {
      const normalizeDisplay = (value) => (value || '').replace(/\\s+/g, ' ').trim();
      const normalizeMatch = (value) => (value || '')
        .normalize('NFKC')
        .replace(/\\s+/g, '')
        .replace(/…|\\.\\.\\./g, '')
        .trim()
        .toLowerCase();
      const needleDisplay = normalizeDisplay(needle);
      const needleMatch = normalizeMatch(needle);
      const shortNeedle = needleMatch.length > 12 ? needleMatch.slice(0, 12) : needleMatch;
      const hasCjk = (value) => /[\\u3400-\\u9fff]/.test(value);
      const viewportArea = Math.max(1, window.innerWidth * window.innerHeight);
      const nodes = Array.from(document.querySelectorAll('a,button,[role=button],[role=treeitem],[role=listitem],[tabindex],div,span,p'));
      const visible = (node) => {
        const style = window.getComputedStyle(node);
        const rect = node.getBoundingClientRect();
        return style.visibility !== 'hidden' && style.display !== 'none' && rect.width > 0 && rect.height > 0;
      };
      const bestClickableFor = (node) => {
        let best = node.closest('a,button,[role=button],[role=treeitem],[role=listitem],[tabindex]:not([tabindex="-1"])');
        if (best && visible(best)) return best;
        let current = node;
        for (let depth = 0; depth < 10 && current; depth += 1, current = current.parentElement) {
          if (!visible(current)) continue;
          const style = window.getComputedStyle(current);
          const rect = current.getBoundingClientRect();
          const text = normalizeDisplay(current.innerText || current.textContent || '');
          if (current.onclick || style.cursor === 'pointer') return current;
          if (depth > 0 && text && text.length <= 260 && rect.width >= 80 && rect.height >= 20 && rect.width * rect.height < viewportArea * 0.55) {
            best = current;
          }
        }
        return best || node;
      };
      const candidates = [];
      for (const node of nodes) {
        const nodeText = normalizeDisplay(node.innerText || node.textContent || '');
        const nodeMatch = normalizeMatch(nodeText);
        if (!nodeText || !nodeMatch) continue;
        if (!visible(node)) continue;
        const maxCandidateTextLength = Math.max(120, needleDisplay.length + 80);
        if (nodeText.length > maxCandidateTextLength) continue;
        let score = 0;
        if (nodeText.includes(needleDisplay)) score = 100;
        else if (nodeMatch.includes(needleMatch)) score = 95;
        else if (needleMatch.includes(nodeMatch) && nodeMatch.length >= 8) score = 85;
        else if (needleMatch.includes(nodeMatch) && nodeMatch.length >= 4 && hasCjk(nodeMatch)) score = 82;
        else if (shortNeedle.length >= 8 && nodeMatch.includes(shortNeedle)) score = 75;
        else if (nodeMatch.length >= 8 && needleMatch.startsWith(nodeMatch.slice(0, Math.min(nodeMatch.length, 12)))) score = 65;
        if (score <= 0) continue;
        const clickable = bestClickableFor(node);
        if (!visible(clickable)) continue;
        const clickableText = normalizeDisplay(clickable.innerText || clickable.textContent || '');
        if (clickableText.length > Math.max(180, needleDisplay.length + 120)) continue;
        const rect = clickable.getBoundingClientRect();
        const area = rect.width * rect.height;
        if (area > viewportArea * 0.45) continue;
        const interactiveBoost = clickable.matches('a,button,[role=button],[role=treeitem],[role=listitem],[tabindex]') || window.getComputedStyle(clickable).cursor === 'pointer' || !!clickable.onclick ? 20 : 0;
        candidates.push({score: score + interactiveBoost, textLength: nodeMatch.length, area, clickable});
      }
      if (!candidates.length) return false;
      candidates.sort((a, b) => b.score - a.score || a.textLength - b.textLength || a.area - b.area);
      const target = candidates[0].clickable;
      target.scrollIntoView({block: 'center', inline: 'center'});
      const rect = target.getBoundingClientRect();
      const x = rect.left + rect.width / 2;
      const y = rect.top + rect.height / 2;
      for (const type of ['pointerdown', 'mousedown', 'pointerup', 'mouseup', 'click']) {
        target.dispatchEvent(new MouseEvent(type, {bubbles: true, cancelable: true, view: window, clientX: x, clientY: y}));
      }
      return true;
    }
    """
    try:
        clicked = bool(page.evaluate(script, text))
        if clicked:
            _settle(page)
        return clicked
    except Exception:
        return False


def _dismiss_blocking_popups(page: Any) -> None:
    try:
        page.keyboard.press("Escape")
    except Exception:
        pass
    script = """
    () => {
      const normalize = (value) => (value || '').replace(/\\s+/g, ' ').trim();
      const nodes = Array.from(document.querySelectorAll('button,[role=button],div,span'));
      const closeLabels = new Set(['×', 'x', 'X', '关闭', 'Close']);
      for (const node of nodes.reverse()) {
        const text = normalize(node.innerText || node.textContent || '');
        const aria = normalize(node.getAttribute('aria-label') || '');
        const title = normalize(node.getAttribute('title') || '');
        if (!closeLabels.has(text) && !closeLabels.has(aria) && !closeLabels.has(title)) continue;
        const style = window.getComputedStyle(node);
        const rect = node.getBoundingClientRect();
        if (style.visibility === 'hidden' || style.display === 'none' || rect.width === 0 || rect.height === 0) continue;
        node.click();
        return true;
      }
      return false;
    }
    """
    try:
        if page.evaluate(script):
            _settle(page)
    except Exception:
        pass


def _settle(page: Any) -> None:
    try:
        page.wait_for_load_state("networkidle", timeout=8_000)
    except Exception as exc:
        if not _is_playwright_timeout(exc):
            raise
    time.sleep(1)


def _is_playwright_timeout(exc: Exception) -> bool:
    return exc.__class__.__name__ == "TimeoutError"


def _notify(notifier: TelegramNotifier, text: str) -> None:
    try:
        notifier.send_text(text)
    except Exception as exc:
        logger.warning("failed to send IMA human downloader notification: %s", exc)
