import pytest

from src.opportunities.collect import (
    _caption_from_description,
    _media_records_from_post_data,
    _normalize_epoch_timestamp,
    _normalize_posted_at,
    _supplement_video_metadata,
    parse_post_url,
)


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (
            "https://www.instagram.com/p/De2UJ_XD2Zj/?utm_source=ig_web_copy_link",
            ("De2UJ_XD2Zj", "https://www.instagram.com/p/De2UJ_XD2Zj/"),
        ),
        (
            "https://instagram.com/reel/Abc_123/",
            ("Abc_123", "https://www.instagram.com/reel/Abc_123/"),
        ),
        (
            "https://www.instagram.com/amacss_utsc/p/DezKA-mRV-M/?utm_source=profile",
            ("DezKA-mRV-M", "https://www.instagram.com/p/DezKA-mRV-M/"),
        ),
    ],
)
def test_parse_supported_post_urls(url, expected):
    assert parse_post_url(url) == expected


@pytest.mark.parametrize(
    "url",
    [
        "http://www.instagram.com/p/De2UJ_XD2Zj/",
        "https://example.com/p/De2UJ_XD2Zj/",
        "https://www.instagram.com/accounts/login/",
        "https://www.instagram.com/p/",
    ],
)
def test_rejects_non_post_urls(url):
    with pytest.raises(ValueError):
        parse_post_url(url)


@pytest.mark.parametrize(
    ("description", "expected"),
    [
        (
            '18 likes, 0 comments - ds3.utsc，October 22, 2026 : "Session info\n'
            '📍 Location: IC-230". ',
            "Session info\n📍 Location: IC-230",
        ),
        ('1 like - club, October 22 : "".', ""),
        ("A description without a quoted caption", None),
        (None, None),
    ],
)
def test_extracts_caption_from_instagram_description(description, expected):
    assert _caption_from_description(description) == expected


def test_normalizes_exact_posted_time_and_ignores_invalid_metadata():
    assert _normalize_posted_at([
        "invalid",
        "2026-10-22T13:40:52.000Z",
    ]) == "2026-10-22T13:40:52Z"
    assert _normalize_posted_at(["October 22, 2026"]) is None
    assert _normalize_epoch_timestamp(1792676450) == "2026-10-22T13:40:50Z"
    assert _normalize_epoch_timestamp(None) is None


def test_media_collection_uses_every_embedded_carousel_item(monkeypatch):
    downloaded = []

    def fake_download(page, source_url, post_id, position):
        downloaded.append((source_url, position))
        return {
            "position": position,
            "type": "image",
            "source_url": source_url,
            "local_path": f"media-{position}.jpg",
            "sha256": "a" * 64,
            "width": 1080,
            "height": 1080,
            "download_status": "downloaded",
        }, None

    monkeypatch.setattr("src.opportunities.collect._download_image", fake_download)
    post_data = {
        "has_media_list": True,
        "media": [
            {"type": "image", "source_url": "https://scontent.cdninstagram.com/slide-1.jpg"},
            {"type": "image", "source_url": "https://scontent.cdninstagram.com/slide-2.jpg"},
            {"type": "image", "source_url": "https://scontent.cdninstagram.com/slide-3.jpg"},
        ],
    }

    media, is_complete = _media_records_from_post_data(object(), "post", post_data)

    assert is_complete is True
    assert [item["position"] for item in media] == [1, 2, 3]
    assert len(downloaded) == 3


def test_media_collection_does_not_claim_completeness_when_an_item_is_missing():
    post_data = {
        "has_media_list": True,
        "media": [{"type": "image", "source_url": None}],
    }

    media, is_complete = _media_records_from_post_data(object(), "post", post_data)

    assert media == []
    assert is_complete is False


def test_preview_image_without_post_media_list_is_not_complete(monkeypatch):
    monkeypatch.setattr(
        "src.opportunities.collect._meta_content",
        lambda page, property_name: "https://scontent.cdninstagram.com/preview.jpg",
    )
    monkeypatch.setattr(
        "src.opportunities.collect._download_image",
        lambda page, source_url, post_id, position: (
            {
                "position": position,
                "type": "image",
                "source_url": source_url,
                "local_path": "preview.jpg",
                "sha256": "a" * 64,
                "width": 1080,
                "height": 1080,
                "download_status": "downloaded",
            },
            None,
        ),
    )

    media, is_complete = _media_records_from_post_data(object(), "post", None)

    assert len(media) == 1
    assert is_complete is False


def test_video_metadata_is_not_duplicated_or_added_from_blob_urls():
    existing = [{
        "position": 1,
        "type": "video",
        "source_url": "https://scontent.cdninstagram.com/video.mp4",
        "local_path": None,
        "sha256": None,
        "width": 1080,
        "height": 1920,
        "download_status": "metadata_only",
    }]
    video_metadata = [
        {
            "source_url": "blob:https://www.instagram.com/temporary",
            "width": 1080,
            "height": 1920,
        },
        {
            "source_url": "https://scontent.cdninstagram.com/video.mp4",
            "width": 1080,
            "height": 1920,
        },
    ]

    assert _supplement_video_metadata(existing, video_metadata) == existing


def test_video_metadata_fallback_uses_usable_https_source_and_dimensions():
    media = [{
        "position": 1,
        "type": "image",
        "source_url": "https://scontent.cdninstagram.com/poster.jpg",
    }]

    result = _supplement_video_metadata(media, [{
        "source_url": "https://scontent.cdninstagram.com/video.mp4",
        "width": 1080,
        "height": 1920,
    }])

    assert result[1] == {
        "position": 2,
        "type": "video",
        "source_url": "https://scontent.cdninstagram.com/video.mp4",
        "local_path": None,
        "sha256": None,
        "width": 1080,
        "height": 1920,
        "download_status": "metadata_only",
    }
