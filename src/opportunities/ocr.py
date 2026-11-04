"""Local EasyOCR processing and per-image result caching."""

import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
from PIL import Image, ImageOps, UnidentifiedImageError
from src.opportunities.ocr_quality import CONFIG, critical_date_row, date_signature, improve_boxes, unresolved_critical_rows


ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = ROOT / "data" / "opportunities"
MODEL_DIR = ROOT / ".local" / "easyocr-models"
LANGUAGES = ["en"]
PREPROCESSING_CONFIG = CONFIG
LOW_CONFIDENCE_MEAN_THRESHOLD = 0.5


def _cache_key(
    image_hash: str,
    engine_version: str,
    languages: list[str],
    preprocessing_config: str,
) -> str:
    payload = json.dumps(
        {
            "image_hash": image_hash,
            "engine": "easyocr",
            "engine_version": engine_version,
            "language_config": languages,
            "preprocessing_config": preprocessing_config,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _result_path(cache_dir: Path, cache_key: str) -> Path:
    return cache_dir / f"{cache_key}.json"


def _cached_result(path: Path, image_hash: str, cache_key: str) -> dict | None:
    if not path.exists():
        return None
    result = json.loads(path.read_text(encoding="utf-8"))
    if result.get("image_hash") != image_hash or result.get("cache_key") != cache_key:
        return None
    return result


def _image_result(
    image_path: Path,
    reader,
    engine_version: str,
    cache_dir: Path,
    languages: list[str],
    preprocessing_config: str,
    processed_at: str,
) -> dict:
    image_bytes = image_path.read_bytes()
    image_hash = hashlib.sha256(image_bytes).hexdigest()
    cache_key = _cache_key(
        image_hash, engine_version, languages, preprocessing_config
    )
    output_path = _result_path(cache_dir, cache_key)
    cached = _cached_result(output_path, image_hash, cache_key)
    if cached is not None:
        return cached

    base = {
        "image_hash": image_hash,
        "cache_key": cache_key,
        "engine": "easyocr",
        "engine_version": engine_version,
        "language_config": languages,
        "preprocessing_config": preprocessing_config,
        "processed_at": processed_at,
    }
    try:
        with Image.open(image_path) as source:
            image = ImageOps.exif_transpose(source).convert("RGB")
            image.load()
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError):
        result = {**base, "status": "corrupt", "text": None, "boxes": []}
    else:
        detections = reader.readtext(np.asarray(image), detail=1, paragraph=False)
        boxes = [
            {
                "text": str(text),
                "confidence": float(confidence),
                "polygon": [
                    {"x": float(point[0]), "y": float(point[1])}
                    for point in polygon
                ],
            }
            for polygon, text, confidence in detections
            if str(text).strip()
        ]
        unresolved_critical = False
        if preprocessing_config == CONFIG:
            boxes, attempts = improve_boxes(image, boxes, reader)
            unresolved_critical = unresolved_critical_rows(boxes, attempts)
            attempts_dir = cache_dir / "attempts"
            attempts_dir.mkdir(parents=True, exist_ok=True)
            (attempts_dir / f"{cache_key}.json").write_text(json.dumps({
                "image_hash": image_hash, "cache_key": cache_key, "processed_at": processed_at,
                "preprocessing_config": preprocessing_config, "attempts": attempts,
            }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        if not boxes:
            status = "empty"
            text = None
        else:
            text = "\n".join(box["text"] for box in boxes)
            mean_confidence = sum(box["confidence"] for box in boxes) / len(boxes)
            weak_critical = any(critical_date_row(box["text"]) and
                                (box["confidence"] < 0.8 or date_signature(box["text"]) is None)
                                for box in boxes)
            status = (
                "low_confidence"
                if mean_confidence < LOW_CONFIDENCE_MEAN_THRESHOLD or weak_critical or unresolved_critical
                else "success"
            )
        result = {**base, "status": status, "text": text, "boxes": boxes}

    cache_dir.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return result


def _image_path(local_path: str) -> Path:
    path = (ROOT / local_path).resolve()
    try:
        path.relative_to(ROOT)
    except ValueError as error:
        raise ValueError(f"Image path escapes the repository: {local_path}") from error
    return path


def process_snapshot(
    snapshot_path: Path,
    reader,
    engine_version: str,
    cache_dir: Path | None = None,
) -> list[dict]:
    post_path = Path(snapshot_path).resolve()
    try:
        post_path.relative_to(ROOT)
    except ValueError as error:
        raise ValueError(f"Snapshot path escapes the repository: {snapshot_path}") from error
    post = json.loads(post_path.read_text(encoding="utf-8"))
    output_dir = cache_dir or DATA_DIR / "ocr"
    processed_at = datetime.now(timezone.utc).isoformat(
        timespec="seconds"
    ).replace("+00:00", "Z")
    results = []
    for media in post["media"]:
        if media["type"] != "image" or media["download_status"] != "downloaded":
            continue
        if not media["local_path"]:
            continue
        results.append(
            _image_result(
                _image_path(media["local_path"]),
                reader,
                engine_version,
                output_dir,
                LANGUAGES,
                PREPROCESSING_CONFIG,
                processed_at,
            )
        )
    return results


def process_post(
    post_id: str,
    reader,
    engine_version: str,
    cache_dir: Path | None = None,
) -> list[dict]:
    return process_snapshot(
        DATA_DIR / f"{post_id}.json",
        reader,
        engine_version,
        cache_dir,
    )


def _load_reader(offline: bool = False):
    try:
        import easyocr
    except ImportError as error:
        raise RuntimeError(
            "EasyOCR is not installed. Install the project requirements in a "
            "supported Python/PyTorch environment before running OCR."
        ) from error
    reader = easyocr.Reader(
        LANGUAGES,
        gpu=False,
        model_storage_directory=str(MODEL_DIR),
        download_enabled=not offline,
    )
    return reader, easyocr.__version__


def main() -> int:
    parser = argparse.ArgumentParser(description="Run local EasyOCR on a collected post.")
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--post-id", help="Collected Instagram post ID")
    target.add_argument(
        "--snapshots",
        nargs="+",
        type=Path,
        help="One or more immutable raw-post snapshot paths.",
    )
    parser.add_argument("--offline", action="store_true", help="Require cached models; never download.")
    parser.add_argument("--json", action="store_true", help="Emit per-snapshot machine-readable status.")
    arguments = parser.parse_args()
    reader, engine_version = _load_reader(arguments.offline)
    snapshots = [DATA_DIR / f"{arguments.post_id}.json"] if arguments.post_id else arguments.snapshots
    reports = {}
    results = []
    for snapshot in snapshots:
        try:
            items = process_snapshot(snapshot, reader, engine_version)
            results.extend(items)
            statuses = [item["status"] for item in items]
            status = ("failed" if "corrupt" in statuses else
                      "low_confidence" if "low_confidence" in statuses else
                      "empty" if "empty" in statuses or not statuses else "complete")
            reports[str(snapshot)] = {"status": status, "image_statuses": statuses}
        except (OSError, ValueError, RuntimeError) as error:
            reports[str(snapshot)] = {"status": "failed", "error": str(error)}
    counts = {}
    for result in results:
        counts[result["status"]] = counts.get(result["status"], 0) + 1
    if arguments.json:
        print(json.dumps({"snapshots": reports, "image_counts": counts}))
    else:
        print(f"OCR processed {len(results)} images: {counts}; snapshots: {reports}")
    return 1 if any(item["status"] != "complete" for item in reports.values()) else 0


if __name__ == "__main__":
    raise SystemExit(main())
