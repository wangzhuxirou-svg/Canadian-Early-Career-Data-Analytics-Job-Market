"""Bounded public Instagram account scanning and opportunity-first collection."""

import json
import re
import subprocess
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo
from urllib.parse import urlparse

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

from src.opportunities.collect import (
    DATA_DIR,
    PROFILE_DIR,
    _caption_from_description,
    _detect_access_block,
    _embedded_post_data,
    _meta_content,
    _normalize_epoch_timestamp,
    _posted_at_from_page,
    _wait_for_signup_prompt_dismissal,
    collect_post_from_page,
    parse_post_url,
    save_post_snapshot,
)
from src.opportunities.extract import EXCLUSION_TERMS, build_draft
from src.opportunities.drafts import store_draft, track_source
from src.opportunities.runner import run_account_checks
from src.opportunities.state import OpportunityState


DEFAULT_LOOKBACK_DAYS = 30
DEFAULT_MAX_POSTS = 24
DEFAULT_MAX_SCROLLS = 6
DEFAULT_MAX_COLLECTED = 5
DEFAULT_MAX_OCR_FALLBACKS = 1
PAGE_DELAY_MS = 1500
ROOT = DATA_DIR.parents[1]
OCR_PYTHON = ROOT / ".venv-ocr" / "Scripts" / "python.exe"
OCR_MODEL_DIR = ROOT / ".local" / "easyocr-models"
LOCAL_ZONE = ZoneInfo("America/Toronto")
ACTION_TERMS = (
    "chance to apply", "submit your application", "submit an application", "apply to join",
    "register", "registration", "sign up", "sign-up", "apply now", "apply today", "apply by",
    "apply through", "apply via", "applications open", "applications are open",
    "applications close", "applications closes", "application deadline",
    "applications are now open", "application required", "deadline to apply",
    "hiring", "we're recruiting", "we are recruiting", "recruiting now",
    "recruitment open", "volunteer",
    "volunteering", "join our team", "join the team", "become a",
    "join us for", "join us at", "come join us for", "rsvp",
    "reserve your spot", "save your spot", "tickets available",
)
EVENT_TERMS = (
    "workshop", "bootcamp", "competition", "hackathon", "datathon", "seminar",
    "talk", "panel", "lecture", "summit", "networking", "career", "conference",
    "general meeting", "sports day", "mentorship", "ambassador", "research",
    "volunteer", "event", "session", "program",
)
PROMOTION_TERMS = (
    "discount", "off at", "% off", "promo code", "special offer",
    "attendee deal", "exclusive deal",
)
MONTH_DAY_PATTERN = re.compile(
    r"\b(?P<month>jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|"
    r"jun(?:e)?|jul(?:y)?|aug(?:ust)?|sep(?:tember)?|oct(?:ober)?|"
    r"nov(?:ember)?|dec(?:ember)?)\s+(?P<day>\d{1,2})(?:st|nd|rd|th)?"
    r"(?:,?\s+(?P<year>20\d{2}))?\b",
    re.IGNORECASE,
)
DAY_MONTH_PATTERN = re.compile(
    r"\b(?P<day>\d{1,2})(?:st|nd|rd|th)?[ \t]*"
    r"(?:\r?\n[ \t]*[^\r\n]*){1,2}\r?\n[ \t]*\b"
    r"(?P<month>jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|"
    r"jun(?:e)?|jul(?:y)?|aug(?:ust)?|sep(?:tember)?|oct(?:ober)?|"
    r"nov(?:ember)?|dec(?:ember)?)\b(?:,?\s+(?P<year>20\d{2}))?",
    re.IGNORECASE,
)
UPCOMING_TERMS = (
    "is coming", "coming soon", "upcoming", "will be held", "will take place",
    "taking place", "is here", "is back", "back this", "returns", "this fall",
    "this winter", "this spring", "program begins", "starts soon", "coming to",
    "invite you to", "invites you to",
)
RETROSPECTIVE_TERMS = (
    *EXCLUSION_TERMS,
    "recap", "thank you to everyone who attended",
    "thank you to those who attended", "thanks to everyone who attended",
    "thank you to everyone who joined", "thank you to those who joined",
    "thanks to everyone who joined",
    "thank you to everyone who applied", "the event was a success",
    "was a success", "was an amazing", "we had the honour of hosting",
    "we had the honor of hosting", "memories from",
)
DATE_SIGNAL = re.compile(
    r"\b(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|"
    r"jul(?:y)?|aug(?:ust)?|sep(?:tember)?|oct(?:ober)?|nov(?:ember)?|"
    r"dec(?:ember)?)\s+\d{1,2}\b|\b20\d{2}-\d{2}-\d{2}\b",
    re.IGNORECASE,
)


class AccessBlockedError(RuntimeError):
    """Raised when Instagram signals a challenge or temporary restriction."""


def _normalize_text(text: str) -> str:
    return " ".join(text.casefold().split())


def opportunity_relevance(caption: str | None, has_image: bool) -> str:
    """Return collect, exclude, or inspect_image without guessing missing text."""
    normalized = _normalize_text(caption or "")
    from src.opportunities.content import exclusion_reason
    if exclusion_reason(caption):
        return "exclude"
    has_action = bool(normalized and any(term in normalized for term in ACTION_TERMS))
    if normalized and any(term in normalized for term in RETROSPECTIVE_TERMS) and not has_action:
        return "exclude"
    if normalized and any(term in normalized for term in PROMOTION_TERMS) and not has_action:
        return "exclude"
    if has_action:
        return "collect"
    has_event_signal = any(term in normalized for term in EVENT_TERMS)
    has_upcoming_signal = (
        any(term in normalized for term in UPCOMING_TERMS)
        or DATE_SIGNAL.search(normalized) is not None
    )
    if has_event_signal and has_upcoming_signal:
        return "inspect_image" if has_image else "collect"
    if caption in {None, ""} and has_image:
        return "inspect_image"
    return "exclude"


def _draft_evidence_text(draft: dict) -> str:
    opportunity = draft.get("opportunity", {})
    return "\n".join(
        [
            str(opportunity.get("description") or ""),
            *(
                str(item.get("evidence_text") or "")
                for item in draft.get("evidence", [])
            ),
        ]
    )


def _post_links(page) -> list[str]:
    hrefs = page.locator("a[href]").evaluate_all(
        "elements => elements.map(element => element.href)"
    )
    links = []
    seen = set()
    for href in hrefs:
        try:
            post_id, canonical_url = parse_post_url(href)
        except ValueError:
            continue
        if post_id not in seen:
            seen.add(post_id)
            links.append(canonical_url)
    return links


def discover_profile_posts(
    page,
    profile_url: str,
    max_posts: int = DEFAULT_MAX_POSTS,
    max_scrolls: int = DEFAULT_MAX_SCROLLS,
    wait_seconds: int = 300,
) -> tuple[list[str], bool]:
    """Scroll the visible profile a bounded amount; bool indicates reaching its end."""
    if max_posts < 1 or max_scrolls < 1:
        raise ValueError("max_posts and max_scrolls must be positive.")
    page.goto(profile_url, wait_until="domcontentloaded", timeout=60000)
    block = _detect_access_block(page)
    if block:
        raise AccessBlockedError(block)
    try:
        _wait_for_signup_prompt_dismissal(page, wait_seconds)
    except RuntimeError as error:
        raise AccessBlockedError(str(error)) from error
    block = _detect_access_block(page)
    if block:
        raise AccessBlockedError(block)
    page.wait_for_timeout(PAGE_DELAY_MS)

    links = []
    seen = set()
    stable_bottoms = 0
    reached_end = False
    for scroll_index in range(max_scrolls + 1):
        for link in _post_links(page):
            if link not in seen:
                seen.add(link)
                links.append(link)
                if len(links) >= max_posts:
                    return links, False
        if scroll_index == max_scrolls:
            break
        before = page.evaluate("document.documentElement.scrollHeight")
        page.evaluate("window.scrollTo(0, document.documentElement.scrollHeight)")
        page.wait_for_timeout(PAGE_DELAY_MS)
        after = page.evaluate("document.documentElement.scrollHeight")
        if after == before:
            stable_bottoms += 1
            if stable_bottoms >= 2:
                reached_end = True
                break
        else:
            stable_bottoms = 0
    for link in _post_links(page):
        if link not in seen:
            seen.add(link)
            links.append(link)
            if len(links) >= max_posts:
                return links, False
    return links, reached_end


def _caption_and_post_data(page, post_id: str) -> tuple[str | None, dict | None]:
    data = _embedded_post_data(page, post_id)
    caption = (
        data.get("caption")
        if data and data.get("caption") is not None
        else _caption_from_description(_meta_content(page, "og:description"))
    )
    return caption, data


def _posted_at(page, post_data: dict | None) -> str | None:
    return (
        _normalize_epoch_timestamp(post_data.get("taken_at"))
        if post_data
        else None
    ) or _posted_at_from_page(page)


def _is_image_post(post_data: dict | None) -> bool:
    return bool(
        post_data
        and any(item.get("type") == "image" for item in post_data.get("media", []))
    )


def _draft_is_expired(
    draft: dict,
    today: date | None = None,
    source_text: str | None = None,
) -> bool:
    opportunity = draft.get("opportunity", {})
    current_date = today or datetime.now(LOCAL_ZONE).date()
    for field in ("start_date", "deadline_date"):
        date_text = opportunity.get(field)
        if not date_text:
            continue
        try:
            event_date = date.fromisoformat(date_text)
        except ValueError:
            continue
        if event_date < current_date:
            return True

    description = source_text or opportunity.get("description") or ""
    normalized = _normalize_text(description)
    month_names = {
        "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
        "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
    }
    candidates = []
    date_matches = [
        (match, match.group("day"), match.group("month"), match.group("year"), False)
        for match in MONTH_DAY_PATTERN.finditer(normalized)
    ]
    date_matches.extend(
        (match, match.group("day"), match.group("month"), match.group("year"), True)
        for match in DAY_MONTH_PATTERN.finditer(description)
    )
    for match, day_text, month_text, year_text, reversed_order in date_matches:
        month = month_names[month_text[:3].casefold()]
        year = int(year_text or current_date.year)
        try:
            candidate = date(year, month, int(day_text))
        except ValueError:
            continue
        if reversed_order:
            before = ""
            surrounding = match.group(0).casefold()
        else:
            before = normalized[max(0, match.start() - 70):match.start()]
            surrounding = normalized[match.start():match.end()]
        is_deadline = any(
            marker in before or marker in surrounding
            for marker in (
                "deadline", "due date", "apply by", "application closes",
                "registration closes", "closes on", " by ",
            )
        )
        candidates.append((candidate, is_deadline))
        if is_deadline and candidate < current_date:
            return True
    if candidates and all(candidate < current_date for candidate, _ in candidates):
        return True
    return False


def _post_evidence_text(raw_post: dict) -> str:
    parts = []
    caption = raw_post.get("caption_raw")
    if raw_post.get("caption_status") == "available" and isinstance(caption, str):
        parts.append(caption)
    image_hashes = {
        media.get("sha256")
        for media in raw_post.get("media", [])
        if media.get("type") == "image" and media.get("sha256")
    }
    if not image_hashes:
        return "\n".join(parts)
    ocr_dir = DATA_DIR / "ocr"
    if ocr_dir.exists():
        for result_path in ocr_dir.glob("*.json"):
            result = json.loads(result_path.read_text(encoding="utf-8"))
            if (
                result.get("image_hash") in image_hashes
                and result.get("status") == "success"
                and result.get("text")
            ):
                parts.append(result["text"])
    return "\n".join(parts)


class InstagramAccountScanner:
    """One visible browser session reused for a bounded scan of selected accounts."""

    def __init__(
        self,
        lookback_days: int = DEFAULT_LOOKBACK_DAYS,
        max_posts: int = DEFAULT_MAX_POSTS,
        max_scrolls: int = DEFAULT_MAX_SCROLLS,
        max_collected: int = DEFAULT_MAX_COLLECTED,
        max_ocr_fallbacks: int = DEFAULT_MAX_OCR_FALLBACKS,
        wait_seconds: int = 300,
        state: OpportunityState | None = None,
    ):
        for name, value in (
            ("lookback_days", lookback_days),
            ("max_posts", max_posts),
            ("max_scrolls", max_scrolls),
            ("max_collected", max_collected),
            ("wait_seconds", wait_seconds),
        ):
            if value < 1:
                raise ValueError(f"{name} must be positive.")
        if max_ocr_fallbacks < 0:
            raise ValueError("max_ocr_fallbacks cannot be negative.")
        self.lookback_days = lookback_days
        self.max_posts = max_posts
        self.max_scrolls = max_scrolls
        self.max_collected = max_collected
        self.max_ocr_fallbacks = max_ocr_fallbacks
        self.wait_seconds = wait_seconds
        self.state = state
        self.playwright = None
        self.context = None
        self.page = None
        self.run_id = None
        self.snapshot_dir = None

    def __enter__(self):
        self.start()
        return self

    def start(self):
        if self.page is not None:
            return
        try:
            self.playwright = sync_playwright().start()
            self.context = self.playwright.chromium.launch_persistent_context(
                str(PROFILE_DIR),
                headless=False,
                accept_downloads=False,
                viewport={"width": 1280, "height": 900},
            )
            self.page = (
                self.context.pages[0] if self.context.pages else self.context.new_page()
            )
        except PlaywrightError as error:
            if self.playwright:
                self.playwright.stop()
            raise RuntimeError(f"Could not start the visible Instagram browser: {error}") from error

    def __exit__(self, exception_type, exception, traceback):
        self.close()

    def close(self):
        if self.context:
            self.context.close()
            self.context = None
        if self.playwright:
            self.playwright.stop()
            self.playwright = None
        self.page = None

    def set_run_id(self, run_id: str):
        self.run_id = run_id
        self.snapshot_dir = DATA_DIR / "snapshots" / run_id

    def finalize(self):
        from src.opportunities.triage import reprocess
        self.triage_counts = reprocess(DATA_DIR)["counts"]

    def __call__(self, account: dict) -> dict:
        self.start()
        if self.page is None:
            raise RuntimeError("InstagramAccountScanner must be used as a context manager.")
        username = account["username"]
        profile_url = account["profile_url"]
        profile_path = urlparse(profile_url)
        if (
            profile_path.hostname not in {"www.instagram.com", "instagram.com"}
            or profile_path.path.strip("/").casefold() != username.casefold()
            or profile_path.query
            or profile_path.fragment
        ):
            raise ValueError("Organizer profile URL is not an Instagram URL.")
        try:
            links, reached_end = discover_profile_posts(
                self.page,
                profile_url,
                max_posts=self.max_posts,
                max_scrolls=self.max_scrolls,
                wait_seconds=self.wait_seconds,
            )
        except PlaywrightTimeoutError as error:
            raise TimeoutError(f"Profile scan timed out for @{username}.") from error
        except AccessBlockedError as error:
            return self._blocked(str(error))
        except PlaywrightError as error:
            raise RuntimeError(f"Browser interaction failed for @{username}: {error}") from error
        block = _detect_access_block(self.page)
        if block:
            return self._blocked(block)
        if "/accounts/login" in self.page.url.casefold() or self.page.locator(
            "input[name='username'], input[name='password']"
        ).count():
            print(
                "Log in manually in the visible browser. Waiting up to "
                f"{self.wait_seconds} seconds before scanning @{username}."
            )
            try:
                self.page.wait_for_function(
                    """() => !location.pathname.startsWith('/accounts/login')
                        && !document.querySelector(
                            "input[name='username'], input[name='password']"
                        )""",
                    timeout=self.wait_seconds * 1000,
                )
                self.page.goto(profile_url, wait_until="domcontentloaded", timeout=60000)
                self.page.wait_for_timeout(PAGE_DELAY_MS)
                links, reached_end = discover_profile_posts(
                    self.page,
                    profile_url,
                    max_posts=self.max_posts,
                    max_scrolls=self.max_scrolls,
                    wait_seconds=self.wait_seconds,
                )
            except PlaywrightTimeoutError:
                block = _detect_access_block(self.page)
                return {
                    "status": "blocked" if block else "requires_auth",
                    "coverage_status": "blocked",
                    "discovered_count": len(links),
                    "collected_count": 0,
                    "stop_run": True,
                    "error": block or (
                        f"Manual login was not completed for @{username}; scan stopped."
                    ),
                }
            except AccessBlockedError as error:
                return self._blocked(str(error))
            except PlaywrightError as error:
                raise RuntimeError(
                    f"Browser interaction failed for @{username}: {error}"
                ) from error

        profile_text = self.page.locator("body").inner_text(timeout=5000).casefold()
        if any(
            phrase in profile_text
            for phrase in (
                "this account is private",
                "sorry, this page isn't available",
                "page isn't available",
            )
        ):
            return {
                "status": "partial",
                "coverage_status": "inaccessible",
                "discovered_count": len(links),
                "collected_count": 0,
                "error": f"Public profile content was not accessible for @{username}.",
            }

        since = datetime.now(timezone.utc) - timedelta(days=self.lookback_days)
        collected = 0
        inspected = 0
        image_fallbacks = 0
        skipped_reasons = {}
        collected_posts = []
        snapshot_records = []
        boundary_unknown = False

        for post_url in links:
            if inspected >= self.max_posts or collected >= self.max_collected:
                reached_end = False
                break
            post_id, canonical_url = parse_post_url(post_url)
            try:
                self.page.goto(
                    canonical_url, wait_until="domcontentloaded", timeout=60000
                )
            except PlaywrightTimeoutError as error:
                raise TimeoutError(f"Post page timed out for {post_id}.") from error
            except PlaywrightError as error:
                raise RuntimeError(f"Browser interaction failed for post {post_id}: {error}") from error
            self.page.wait_for_timeout(PAGE_DELAY_MS)
            block = _detect_access_block(self.page)
            if block:
                return self._blocked(block, len(links), collected)
            post_data = _embedded_post_data(self.page, post_id)
            if (
                post_data
                and post_data.get("author_username")
                and post_data["author_username"].casefold() != username.casefold()
            ):
                skipped_reasons["author_mismatch"] = skipped_reasons.get(
                    "author_mismatch", 0
                ) + 1
                continue
            caption, post_data = _caption_and_post_data(self.page, post_id)
            posted_at = _posted_at(self.page, post_data)
            if not posted_at:
                boundary_unknown = True
                skipped_reasons["publication_time_unavailable"] = skipped_reasons.get(
                    "publication_time_unavailable", 0
                ) + 1
                continue
            if datetime.fromisoformat(posted_at.replace("Z", "+00:00")) < since:
                skipped_reasons["outside_lookback"] = skipped_reasons.get(
                    "outside_lookback", 0
                ) + 1
                continue

            relevance = opportunity_relevance(caption, _is_image_post(post_data))
            known_draft = (DATA_DIR / "drafts" / f"{post_id}.json").is_file()
            if relevance == "exclude" and not known_draft:
                skipped_reasons["no_opportunity_signal_or_retrospective"] = (
                    skipped_reasons.get("no_opportunity_signal_or_retrospective", 0) + 1
                )
                inspected += 1
                continue
            if relevance == "inspect_image" and not known_draft:
                if image_fallbacks >= self.max_ocr_fallbacks:
                    skipped_reasons["image_only_fallback_limit"] = skipped_reasons.get(
                        "image_only_fallback_limit", 0
                    ) + 1
                    inspected += 1
                    continue
                image_fallbacks += 1

            try:
                post = collect_post_from_page(
                    self.page, account["organizer_id"], post_id, canonical_url
                )
            except RuntimeError as error:
                if "HTTP 429" in str(error):
                    return self._blocked(str(error), len(links), collected)
                raise
            path = save_post_snapshot(
                post,
                snapshot_dir=self.snapshot_dir,
            )
            revision = None
            if self.state is not None and self.run_id:
                revision = self.state.record_snapshot(self.run_id, post, path)["content_revision"]
            version = track_source(DATA_DIR, post, revision)
            collected += 1
            inspected += 1
            collected_record = {
                "source_post_id": post_id,
                "source_url": canonical_url,
                "caption_status": post["caption_status"],
                "collection_status": post["collection_status"],
                "selection_reason": relevance,
                "ocr_status": "not_needed",
                "draft_status": "not_created",
                "content_revision": version["content_revision"],
            }
            collected_posts.append(collected_record)
            snapshot_records.append((post, path, collected_record))
            collected_record["_version"] = version

        image_snapshots = [
            path
            for post, path, _record in snapshot_records
            if any(
                item["type"] == "image" and item["download_status"] == "downloaded"
                for item in post["media"]
            )
        ]
        ocr_result = self._run_local_ocr(image_snapshots) if image_snapshots else {}
        for post, _path, record in snapshot_records:
            version = record.pop("_version")
            ocr_detail = (ocr_result.get(str(_path), {"status": "failed", "error": "Missing OCR result"})
                          if isinstance(ocr_result, dict) else {"status": "failed", "error": ocr_result})
            has_downloaded_image = any(
                item["type"] == "image" and item["download_status"] == "downloaded"
                for item in post["media"]
            )
            if has_downloaded_image and ocr_detail["status"] == "failed":
                record["ocr_status"] = "failed"
                record["ocr_error"] = ocr_detail.get("error", "One or more images could not be read.")
            if has_downloaded_image:
                record["ocr_status"] = ocr_detail["status"]
                record["ocr_image_statuses"] = ocr_detail.get("image_statuses", [])
            from src.opportunities.triage import process_local_post
            version["processing_issues"] = (["ocr_failed_this_run"] if record["ocr_status"] == "failed" else [])
            decision = process_local_post(DATA_DIR, post, version=version)
            record["draft_status"] = decision["decision"]
            record["triage_reasons"] = decision["reasons"]

        for record in collected_posts:
            metadata_path = DATA_DIR / "draft_versions" / f"{record['source_post_id']}.json"
            if metadata_path.exists():
                record["content_revision"] = json.loads(metadata_path.read_text(encoding="utf-8"))["content_revision"]
        coverage_complete = (
            reached_end
            and not boundary_unknown
            and not skipped_reasons.get("image_only_fallback_limit")
            and all(record["ocr_status"] in {"complete", "not_needed"} for record in collected_posts)
            and all(record["collection_status"] == "complete" for record in collected_posts)
        )
        status = "success" if coverage_complete else "partial"
        return {
            "status": status,
            "coverage_status": "complete" if coverage_complete else "partial",
            "discovered_count": len(links),
            "collected_count": collected,
            "collected_posts": collected_posts,
            "lookback_days": self.lookback_days,
            "skipped_reasons": skipped_reasons,
            "error": (
                "; ".join(
                    value
                    for value in [
                        "Some image text is empty or low confidence and requires manual inspection."
                        if any(record["ocr_status"] in {"empty", "low_confidence"} for record in collected_posts)
                        else None,
                        "One or more collected snapshots are incomplete."
                        if any(record["collection_status"] != "complete" for record in collected_posts)
                        else None,
                        "Local OCR failed for one or more collected image posts."
                        if any(record["ocr_status"] == "failed" for record in collected_posts)
                        else None,
                        "Bounded scan did not confirm complete profile coverage within the "
                        "configured discovery limits."
                        if not reached_end or boundary_unknown
                        else None,
                    ]
                    if value
                )
                or None
            ),
        }

    @staticmethod
    def _run_local_ocr(snapshot_paths: list[Path]) -> dict | str:
        expected_models = (
            OCR_MODEL_DIR / "craft_mlt_25k.pth",
            OCR_MODEL_DIR / "english_g2.pth",
        )
        if not OCR_PYTHON.is_file() or not all(path.is_file() for path in expected_models):
            return (
                "OCR was not run: the isolated OCR Python environment or cached models "
                "are missing; no model download was attempted."
            )
        try:
            completed = subprocess.run(
                [
                    str(OCR_PYTHON),
                    "-m",
                    "src.opportunities.ocr",
                    "--offline",
                    "--json",
                    "--snapshots",
                    *[str(path) for path in snapshot_paths],
                ],
                cwd=ROOT,
                check=False,
                capture_output=True,
                text=True,
                timeout=900,
            )
        except subprocess.TimeoutExpired:
            return "Local OCR exceeded its 15-minute limit."
        try:
            report = json.loads(completed.stdout.strip().splitlines()[-1])["snapshots"]
            if not isinstance(report, dict) or any(
                not isinstance(item, dict) or item.get("status") not in
                {"complete", "failed", "empty", "low_confidence"} for item in report.values()
            ):
                raise ValueError("Invalid per-snapshot OCR report")
            return report
        except (ValueError, KeyError, IndexError):
            return (completed.stderr or completed.stdout or "OCR produced no status report")[-1000:]

    @staticmethod
    def _blocked(message: str, discovered: int = 0, collected: int = 0) -> dict:
        return {
            "status": "blocked",
            "coverage_status": "blocked",
            "discovered_count": discovered,
            "collected_count": collected,
            "stop_run": True,
            "error": message,
        }


def run_live_scan(
    accounts: list[dict] | None = None,
    state: OpportunityState | None = None,
    **scanner_options,
) -> dict:
    state = state or OpportunityState()
    scanner = InstagramAccountScanner(state=state, **scanner_options)
    try:
        result = run_account_checks(
            checker=scanner,
            accounts=accounts,
            state=state,
        )
        result["opportunity_triage_counts"] = getattr(scanner, "triage_counts", {})
        return result
    finally:
        scanner.close()
