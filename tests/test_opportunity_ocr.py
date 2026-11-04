import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry, Resource
from PIL import Image

from src.opportunities.ocr import _image_result


ROOT = Path(__file__).resolve().parents[1]


class FakeReader:
    def __init__(self, detections):
        self.detections = detections
        self.calls = 0

    def readtext(self, image, detail, paragraph, **options):
        self.calls += 1
        assert image.ndim == 3
        assert detail == 1
        assert paragraph is False
        return self.detections


def validate_ocr_result(result):
    schema_dir = ROOT / "contracts" / "schemas"
    schemas = {
        path.name.removesuffix(".schema.json"): json.loads(
            path.read_text(encoding="utf-8")
        )
        for path in schema_dir.glob("*.schema.json")
    }
    registry = Registry().with_resources(
        (schema["$id"], Resource.from_contents(schema))
        for schema in schemas.values()
    )
    Draft202012Validator(
        schemas["ocr-result"],
        registry=registry,
        format_checker=FormatChecker(),
    ).validate(result)


def test_ocr_creates_schema_valid_boxes_and_reuses_cache(tmp_path):
    image_path = tmp_path / "poster.png"
    Image.new("RGB", (40, 20), color="white").save(image_path)
    reader = FakeReader([
        ([(1, 2), (20, 2), (20, 10), (1, 10)], "Workshop", 0.9),
    ])

    first = _image_result(
        image_path, reader, "test-version", tmp_path / "cache",
        ["en"], "exif_transpose_rgb_v1", "2026-10-28T12:00:00Z",
    )
    second = _image_result(
        image_path, reader, "test-version", tmp_path / "cache",
        ["en"], "exif_transpose_rgb_v1", "2026-10-28T12:01:00Z",
    )

    validate_ocr_result(first)
    assert first["status"] == "success"
    assert first["text"] == "Workshop"
    assert second == first
    assert reader.calls == 1


@pytest.mark.parametrize(
    ("detections", "expected_status", "expected_text"),
    [
        ([], "empty", None),
        (
            [([(0, 0), (10, 0), (10, 10), (0, 10)], "Unclear", 0.2)],
            "low_confidence",
            "Unclear",
        ),
    ],
)
def test_ocr_records_empty_and_low_confidence_results(
    tmp_path, detections, expected_status, expected_text
):
    image_path = tmp_path / "poster.png"
    Image.new("RGB", (10, 10), color="white").save(image_path)

    result = _image_result(
        image_path, FakeReader(detections), "test-version", tmp_path / "cache",
        ["en"], "exif_transpose_rgb_v1", "2026-10-28T12:00:00Z",
    )

    validate_ocr_result(result)
    assert result["status"] == expected_status
    assert result["text"] == expected_text


def test_ocr_records_corrupt_images_without_calling_reader(tmp_path):
    image_path = tmp_path / "broken.png"
    image_path.write_bytes(b"not an image")
    reader = FakeReader([])

    result = _image_result(
        image_path, reader, "test-version", tmp_path / "cache",
        ["en"], "exif_transpose_rgb_v1", "2026-10-28T12:00:00Z",
    )

    validate_ocr_result(result)
    assert result["status"] == "corrupt"
    assert reader.calls == 0


def test_spacing_normalization_does_not_guess_ambiguous_digit_glyphs():
    from src.opportunities.ocr_quality import normalize_date_spacing, date_signature
    assert normalize_date_spacing("BYOCT7THAT1159PM") == "BY OCT 7TH AT 11:59PM"
    assert date_signature("BYOCT7THAT1159PM") == ("OCT", 7)
    assert date_signature("BY OCT71HAT1159PM") is None


def test_weak_deadline_row_cannot_hide_behind_high_average_confidence(tmp_path):
    from src.opportunities.ocr_quality import CONFIG
    path = tmp_path / "poster.png"
    Image.new("RGB", (100, 100), color="white").save(path)
    polygon = [(5, 20), (90, 20), (90, 40), (5, 40)]
    reader = FakeReader([(polygon, "BY OCT71HAT1159PM", 0.74),
                         ([(5, 60), (90, 60), (90, 80), (5, 80)], "HIRING ASSOCIATES", 0.99)])
    result = _image_result(path, reader, "test-version", tmp_path / "cache", ["en"], CONFIG, "2026-10-28T12:00:00Z")
    assert result["status"] == "low_confidence"
    assert reader.calls == 4  # One initial pass and three bounded ROI retries.
    validate_ocr_result(result)
    audit = json.loads(next((tmp_path / "cache/attempts").glob("*.json")).read_text())
    assert len(audit["attempts"]) == 4


def test_split_retry_fragments_do_not_silently_turn_bad_date_into_success(tmp_path):
    from src.opportunities.ocr_quality import CONFIG
    path = tmp_path / "poster.png"
    Image.new("RGB", (100, 60), color="white").save(path)
    class SplitReader:
        calls = 0
        def readtext(self, image, **options):
            self.calls += 1
            if self.calls == 1:
                return [([(5, 5), (90, 5), (90, 40), (5, 40)], "BY OCT71HAT1159PM", .74)]
            return [([(5, 5), (35, 5), (35, 20), (5, 20)], "BY", .99),
                    ([(40, 5), (70, 5), (70, 20), (40, 20)], "OCT", .99)]
    result = _image_result(path, SplitReader(), "test", tmp_path / "cache", ["en"], CONFIG, "2026-10-28T12:00:00Z")
    assert result["status"] == "low_confidence"


def test_recovered_critical_row_preserves_audit_and_cache_version(tmp_path, monkeypatch):
    import src.opportunities.ocr_quality as quality
    path = tmp_path / "poster.png"
    Image.new("RGB", (100, 60), color="white").save(path)
    reader = FakeReader([([(5, 5), (90, 5), (90, 40), (5, 40)], "BY OCT71HAT1159PM", .74)])
    def recover(image, old, reader):
        fixed = {**old, "text": "BY OCT 7 AT 11:59PM", "confidence": .98}
        return fixed, {"pass": "critical_fields", "boxes": [fixed], "field_readings": []}
    monkeypatch.setattr(quality, "_recover_date_line", recover)
    result = _image_result(path, reader, "test", tmp_path / "cache", ["en"], quality.CONFIG, "2026-10-28T12:00:00Z")
    assert result["status"] == "success"
    assert result["text"] == "BY OCT 7 AT 11:59PM"
    assert result["boxes"][0]["polygon"][0] == {"x": 5, "y": 5}
    validate_ocr_result(result)
    assert quality.CONFIG != "exif_transpose_rgb_v1"
