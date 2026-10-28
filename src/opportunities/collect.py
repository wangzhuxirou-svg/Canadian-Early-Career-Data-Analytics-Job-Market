"""Single-post Instagram collection using a visible, locally persisted browser profile."""

import argparse
import hashlib
import json
import re
import uuid
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from urllib.parse import urlparse

from PIL import Image
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright
from src.opportunities.drafts import atomic_json


ROOT = Path(__file__).resolve().parents[2]
PROFILE_DIR = ROOT / ".local" / "instagram-profile"
DATA_DIR = ROOT / "data" / "opportunities"
POST_PATH_PATTERN = re.compile(
    r"^/(?:[A-Za-z0-9._]+/)?(p|reel|tv)/([A-Za-z0-9_-]+)/?$"
)
MAX_CAROUSEL_ITEMS = 20


def parse_post_url(url: str) -> tuple[str, str]:
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.netloc.casefold() not in {
        "instagram.com",
        "www.instagram.com",
    }:
        raise ValueError("Post URL must use https://www.instagram.com.")
    match = POST_PATH_PATTERN.fullmatch(parsed.path)
    if not match:
        raise ValueError("URL must point directly to an Instagram post, reel, or video.")
    post_type, post_id = match.groups()
    return post_id, f"https://www.instagram.com/{post_type}/{post_id}/"


def _detect_access_block(page) -> str | None:
    url = page.url.casefold()
    if any(part in url for part in ("/challenge/", "/checkpoint/", "/accounts/suspended/")):
        return "Instagram requires an account challenge or checkpoint; collection stopped."
    if page.locator("input[name='captcha'], iframe[src*='captcha']").count():
        return "A CAPTCHA was detected; collection stopped without attempting to bypass it."
    body_text = page.locator("body").inner_text(timeout=5000).casefold()
    if "try again later" in body_text or "we restrict certain activity" in body_text:
        return "Instagram reports a temporary access restriction; collection stopped."
    return None


def _meta_content(page, property_name: str) -> str | None:
    locator = page.locator(f"meta[property='{property_name}']")
    return locator.get_attribute("content") if locator.count() else None


def _caption_from_description(description: str | None) -> str | None:
    if not description:
        return None
    match = re.search(r':\s*"(.*)"\s*\.?\s*$', description, re.DOTALL)
    return match.group(1) if match else None


def _posted_at_from_page(page) -> str | None:
    timestamps = page.locator("time[datetime]").evaluate_all(
        "elements => elements.map(element => element.getAttribute('datetime'))"
    )
    return _normalize_posted_at(timestamps)


def _normalize_posted_at(timestamps: list[str | None]) -> str | None:
    for timestamp in timestamps:
        if not timestamp:
            continue
        try:
            parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        except ValueError:
            continue
        if parsed.tzinfo is not None:
            return parsed.astimezone(timezone.utc).isoformat(
                timespec="seconds"
            ).replace("+00:00", "Z")
    return None


def _normalize_epoch_timestamp(timestamp: int | float | None) -> str | None:
    if isinstance(timestamp, bool) or not isinstance(timestamp, (int, float)):
        return None
    try:
        return datetime.fromtimestamp(timestamp, timezone.utc).isoformat(
            timespec="seconds"
        ).replace("+00:00", "Z")
    except (OverflowError, OSError, ValueError):
        return None


def _embedded_post_data(page, post_id: str) -> dict | None:
    matches = page.evaluate(
        """postId => {
            const matches = [];
            for (const script of document.querySelectorAll('script[type="application/json"]')) {
                let root;
                try {
                    root = JSON.parse(script.textContent || '');
                } catch {
                    continue;
                }
                const stack = [root];
                while (stack.length) {
                    const value = stack.pop();
                    if (!value || typeof value !== 'object') continue;
                    if (value.code === postId && value.if_not_gated_logged_out) {
                        const post = value.if_not_gated_logged_out;
                        const media = Array.isArray(post.carousel_media)
                            ? post.carousel_media
                            : post.image_versions2 || post.video_versions ? [post] : [];
                        matches.push({
                            author_username: post.user?.username || null,
                            caption: typeof post.caption?.text === 'string'
                                ? post.caption.text
                                : null,
                            taken_at: post.taken_at ?? null,
                            has_media_list: Array.isArray(post.carousel_media),
                            media: media.map(item => {
                                const isVideo = item.media_type === 2
                                    || item.__typename?.toLowerCase().includes('video');
                                const candidates = isVideo
                                    ? item.video_versions || []
                                    : item.image_versions2?.candidates || [];
                                return {
                                    type: isVideo ? 'video' : 'image',
                                    source_url: candidates[0]?.url || item.display_uri || null,
                                    width: item.original_width || null,
                                    height: item.original_height || null
                                };
                            })
                        });
                    }
                    if (Array.isArray(value)) stack.push(...value);
                    else stack.push(...Object.values(value));
                }
            }
            return matches;
        }""",
        post_id,
    )
    return next(
        (
            match for match in matches
            if match["has_media_list"] or match["caption"] is not None
        ),
        matches[0] if matches else None,
    )


def _media_records_from_post_data(
    page, post_id: str, post_data: dict | None
) -> tuple[list[dict], bool]:
    preview_only = not post_data or not post_data.get("media")
    if not post_data or not post_data.get("media"):
        fallback_url = _meta_content(page, "og:image")
        if not fallback_url:
            return [], False
        post_data = {
            "media": [{"type": "image", "source_url": fallback_url}],
            "has_media_list": False,
        }

    media_items = post_data["media"]
    expected_count = len(media_items)
    if expected_count > MAX_CAROUSEL_ITEMS:
        media_items = media_items[:MAX_CAROUSEL_ITEMS]

    records = []
    seen_sources = set()
    complete = (
        not preview_only
        and expected_count > 0
        and expected_count <= MAX_CAROUSEL_ITEMS
    )
    for position, item in enumerate(media_items, start=1):
        source_url = item.get("source_url")
        if not source_url:
            complete = False
            continue
        if item["type"] == "video":
            records.append({
                "position": position,
                "type": "video",
                "source_url": source_url,
                "local_path": None,
                "sha256": None,
                "width": item.get("width"),
                "height": item.get("height"),
                "download_status": "metadata_only",
            })
            continue
        source_path = urlparse(source_url).path
        if not source_path or source_path in seen_sources:
            complete = False
            continue
        seen_sources.add(source_path)
        image, image_error = _download_image(page, source_url, post_id, position)
        if item.get("width"):
            image["width"] = image["width"] or item["width"]
        if item.get("height"):
            image["height"] = image["height"] or item["height"]
        records.append(image)
        if image_error:
            complete = False
    complete = complete and len(records) == expected_count
    return records, complete


def _supplement_video_metadata(media: list[dict], videos: list[dict]) -> list[dict]:
    if any(item["type"] == "video" for item in media):
        return media

    for video in videos:
        source_url = video.get("source_url")
        if not source_url or urlparse(source_url).scheme != "https":
            continue
        media.append({
            "position": len(media) + 1,
            "type": "video",
            "source_url": source_url,
            "local_path": None,
            "sha256": None,
            "width": video.get("width"),
            "height": video.get("height"),
            "download_status": "metadata_only",
        })
    return media


def _wait_for_manual_login(page, post_id: str, wait_seconds: int) -> None:
    print(
        "Log in manually in the browser, then return to the supplied post. "
        f"Waiting up to {wait_seconds} seconds."
    )
    try:
        page.wait_for_function(
            """postId => {
                const postPath = new RegExp(`^/(?:p|reel|tv)/${postId}/?$`);
                const loginForm = document.querySelector(
                    "input[name='username'], input[name='password']"
                );
                return postPath.test(location.pathname) && !loginForm;
            }""",
            arg=re.escape(post_id),
            timeout=wait_seconds * 1000,
        )
    except PlaywrightTimeoutError as error:
        block = _detect_access_block(page)
        if block:
            raise RuntimeError(block) from error
        raise RuntimeError(
            "The post did not become accessible after manual login; no data was saved."
        ) from error


def _wait_for_signup_prompt_dismissal(page, wait_seconds: int) -> None:
    prompt_visible = page.evaluate(
        """() => [...document.querySelectorAll('[role="dialog"] a')].some(link =>
            /\\/accounts\\/(?:login|emailsignup)\\//.test(link.getAttribute('href') || '')
        )"""
    )
    if not prompt_visible:
        return

    print(
        "Instagram is showing a login or signup prompt over the post. "
        "Dismiss it or log in manually in the browser; waiting up to "
        f"{wait_seconds} seconds."
    )
    try:
        page.wait_for_function(
            """() => ![...document.querySelectorAll('[role="dialog"] a')].some(link =>
                /\\/accounts\\/(?:login|emailsignup)\\//.test(link.getAttribute('href') || '')
            )""",
            timeout=wait_seconds * 1000,
        )
    except PlaywrightTimeoutError as error:
        raise RuntimeError(
            "The login/signup prompt remained over the post; collection stopped without "
            "saving an incomplete replacement."
        ) from error


def _download_image(
    page, image_url: str, post_id: str, position: int
) -> tuple[dict, str | None]:
    media = {
        "position": position,
        "type": "image",
        "source_url": image_url,
        "local_path": None,
        "sha256": None,
        "width": None,
        "height": None,
        "download_status": "failed",
    }
    try:
        response = page.request.get(image_url, timeout=30000)
        if response.status == 429:
            raise RuntimeError(
                "Instagram returned HTTP 429 while loading post media; collection stopped."
            )
        if not response.ok:
            return media, f"Image download returned HTTP {response.status}."
        content_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        if not content_type.startswith("image/"):
            return media, f"Image URL returned unexpected content type {content_type or 'unknown'}."
        content = response.body()
        with Image.open(BytesIO(content)) as image:
            image.verify()
        with Image.open(BytesIO(content)) as image:
            width, height = image.size
            image_format = (image.format or "").lower()
        extension = {"jpeg": ".jpg", "png": ".png", "webp": ".webp", "gif": ".gif"}.get(
            image_format, ".img"
        )
        relative_path = (
            Path("data") / "opportunities" / "media" / f"{post_id}-{position}{extension}"
        )
        destination = ROOT / relative_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(content)
        media.update({
            "local_path": relative_path.as_posix(),
            "sha256": hashlib.sha256(content).hexdigest(),
            "width": width,
            "height": height,
            "download_status": "downloaded",
        })
        return media, None
    except (OSError, ValueError, PlaywrightError) as error:
        return media, f"Image could not be downloaded, decoded, or saved: {error}"


def collect_post_from_page(
    page,
    organizer_id: str,
    post_id: str,
    canonical_url: str,
) -> dict:
    timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    errors = []
    post_data = _embedded_post_data(page, post_id)
    caption_raw = (
        post_data.get("caption")
        if post_data and post_data.get("caption") is not None
        else _caption_from_description(_meta_content(page, "og:description"))
    )
    caption_status = (
        "empty" if caption_raw == "" else "available" if caption_raw is not None
        else "unavailable"
    )
    if caption_status == "unavailable":
        errors.append("Full caption was not collected from the accessible post page.")

    posted_at = (
        _normalize_epoch_timestamp(post_data.get("taken_at"))
        if post_data
        else None
    ) or _posted_at_from_page(page)
    if posted_at is None:
        errors.append("Exact post publication time was not available in page time metadata.")

    media, media_completeness_confirmed = _media_records_from_post_data(
        page, post_id, post_data
    )
    if not media:
        errors.append("No post media could be identified.")
    if not media_completeness_confirmed:
        errors.append(
            "The accessible post data did not confirm every media item and successful "
            "download."
        )

    video_metadata = page.locator("video").evaluate_all(
        """elements => elements.map(element => ({
            source_url: element.currentSrc || element.src,
            width: element.videoWidth || null,
            height: element.videoHeight || null
        }))"""
    )
    media = _supplement_video_metadata(media, video_metadata)
    has_video = any(item["type"] == "video" for item in media)
    if has_video:
        errors.append("Video content is metadata-only; video downloading is not implemented.")

    media_completeness = (
        "complete"
        if media_completeness_confirmed and not has_video
        else "unknown"
    )
    return {
        "source": "instagram",
        "source_post_id": post_id,
        "organizer_id": organizer_id,
        "source_url": canonical_url,
        "posted_at": posted_at,
        "caption_raw": caption_raw,
        "caption_status": caption_status,
        "media": media,
        "media_completeness": media_completeness,
        "first_seen_at": timestamp,
        "last_seen_at": timestamp,
        "last_fetched_at": timestamp,
        "content_hash": hashlib.sha256(
            json.dumps(
                {"caption_raw": caption_raw, "media": [item["sha256"] for item in media]},
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest(),
        "collection_status": (
            "complete"
            if caption_status in {"available", "empty"}
            and posted_at is not None
            and media_completeness == "complete"
            and all(item["download_status"] == "downloaded" for item in media)
            else "partial"
        ),
        "errors": errors,
    }


def save_post_snapshot(
    post: dict,
    snapshot_dir: Path | None = None,
    replace_current: bool = True,
) -> Path:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    current_path = DATA_DIR / f"{post['source_post_id']}.json"
    if current_path.exists():
        previous = json.loads(current_path.read_text(encoding="utf-8"))
        post["first_seen_at"] = previous.get("first_seen_at", post["first_seen_at"])
        if (
            post["collection_status"] != "complete"
            and previous.get("collection_status") == "complete"
        ):
            replace_current = False
    snapshot_path = current_path
    if snapshot_dir is not None:
        snapshot_dir.mkdir(parents=True, exist_ok=True)
        snapshot_path = snapshot_dir / f"{post['source_post_id']}.json"
        if snapshot_path.exists():
            snapshot_path = snapshot_dir / f"{post['source_post_id']}_{uuid.uuid4().hex}.json"
        atomic_json(snapshot_path, post)
    if replace_current:
        atomic_json(current_path, post)
    return snapshot_path


def collect_post(url: str, organizer_id: str, wait_seconds: int = 300) -> Path:
    post_id, canonical_url = parse_post_url(url)
    if not organizer_id:
        raise ValueError("An organizer ID is required.")

    with sync_playwright() as playwright:
        context = playwright.chromium.launch_persistent_context(
            str(PROFILE_DIR),
            headless=False,
            accept_downloads=False,
            viewport={"width": 1280, "height": 900},
        )
        try:
            page = context.pages[0] if context.pages else context.new_page()
            page.goto(canonical_url, wait_until="domcontentloaded", timeout=60000)
            block = _detect_access_block(page)
            if block:
                raise RuntimeError(block)

            _wait_for_signup_prompt_dismissal(page, wait_seconds)
            login_visible = page.locator(
                "input[name='username'], input[name='password']"
            ).count() > 0
            if "/accounts/login" in page.url.casefold() or login_visible:
                _wait_for_manual_login(page, post_id, wait_seconds)

            block = _detect_access_block(page)
            if block:
                raise RuntimeError(block)
            page.wait_for_timeout(2000)
            post = collect_post_from_page(
                page, organizer_id, post_id, canonical_url
            )
            return save_post_snapshot(post)
        finally:
            context.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Try collecting one accessible Instagram post.")
    parser.add_argument("--url", required=True, help="Instagram post URL")
    parser.add_argument("--organizer-id", required=True, help="Organizer ID from organizers.json")
    parser.add_argument("--wait-seconds", type=int, default=300)
    arguments = parser.parse_args()
    if arguments.wait_seconds < 1:
        parser.error("--wait-seconds must be positive.")
    output = collect_post(arguments.url, arguments.organizer_id, arguments.wait_seconds)
    post = json.loads(output.read_text(encoding="utf-8"))
    print(
        f"Saved a {post['collection_status']} raw-post snapshot "
        f"with {len(post['media'])} media items to {output}"
    )


if __name__ == "__main__":
    main()
