"""Bounded OCR retries for weak critical rows, with original-coordinate evidence."""

import math
import re

import numpy as np
from PIL import Image, ImageOps

CONFIG = "exif_rgb_critical_fields_deskew_v3"
MONTH = r"(?:JAN(?:UARY)?|FEB(?:RUARY)?|MAR(?:CH)?|APR(?:IL)?|MAY|JUN(?:E)?|JUL(?:Y)?|AUG(?:UST)?|SEP(?:TEMBER)?|OCT(?:OBER)?|NOV(?:EMBER)?|DEC(?:EMBER)?)"


def normalize_date_spacing(text: str) -> str:
    """Repair spacing/punctuation only, never substitute guessed digit/letter glyphs."""
    text = re.sub(r"\bBY(?=" + MONTH + r"\d)", "BY ", text, flags=re.I)
    text = re.sub(r"\b(" + MONTH + r")(?=\d)", r"\1 ", text, flags=re.I)
    text = re.sub(r"(\d(?:ST|ND|RD|TH))(?=AT(?:\d|\b))", r"\1 ", text, flags=re.I)
    text = re.sub(r"(?<=\d)(?=AT\d)", " ", text, flags=re.I)
    text = re.sub(r"\bAT(?=\d)", "AT ", text, flags=re.I)
    def clock(match):
        digits, meridiem = match.group(1), match.group(2)
        hour, minute = int(digits[:-2]), int(digits[-2:])
        return f"{hour}:{minute:02d}{meridiem}" if 1 <= hour <= 12 and minute <= 59 else match.group(0)
    return re.sub(r"\b(\d{3,4})\s*([AP]M)\b", clock, text, flags=re.I)


def date_signature(text):
    normalized = normalize_date_spacing(text)
    match = re.search(r"\b(" + MONTH + r")\s+(\d{1,2})(?:ST|ND|RD|TH)?\b", normalized, re.I)
    if match and 1 <= int(match.group(2)) <= 31:
        return match.group(1)[:3].upper(), int(match.group(2))
    return None


def critical_row(text):
    return bool(re.search(r"\b(?:BY\s*" + MONTH + r"|DEADLINE|CLOSES?|DUE\b)", text, re.I))


def critical_date_row(text):
    return critical_row(text) and bool(re.search(MONTH + r"\s*\d", normalize_date_spacing(text), re.I))


def _bounds(box):
    xs = [point["x"] for point in box["polygon"]]
    ys = [point["y"] for point in box["polygon"]]
    return min(xs), min(ys), max(xs), max(ys)


def _overlap(a, b):
    ax, ay, ar, ab = _bounds(a)
    bx, by, br, bb = _bounds(b)
    intersection = max(0, min(ar, br) - max(ax, bx)) * max(0, min(ab, bb) - max(ay, by))
    return intersection / max(1, min((ar - ax) * (ab - ay), (br - bx) * (bb - by)))


def unresolved_critical_rows(boxes, attempts):
    original = attempts[0]["boxes"] if attempts else boxes
    weak = [box for box in original if critical_date_row(box["text"]) and
            (box["confidence"] < 0.8 or date_signature(box["text"]) is None)]
    return any(not any(_overlap(old, new) > 0.4 and critical_date_row(new["text"])
                       and date_signature(new["text"]) and new["confidence"] >= 0.8
                       for new in boxes) for old in weak)


def _recover_date_line(image, box, reader):
    """Read month/day separately so a small ordinal cannot become a second digit."""
    if not hasattr(reader, "recognize"):
        return None
    try:
        import cv2
    except ImportError:
        return None
    left, top, right, bottom = _bounds(box)
    region = (max(0, int(left) - 6), max(0, int(top) - 6),
              min(image.width, int(right) + 6), min(image.height, int(bottom) + 6))
    crop = image.crop(region)
    gray = np.asarray(ImageOps.grayscale(crop))
    # This branch is for dark print on a light/color banner. Other layouts
    # retain the general contrast/rotation fallback rather than a guessed mask.
    mask = gray < 50
    if mask.mean() < 0.02 or mask.mean() > 0.45:
        return None
    points = np.column_stack(np.where(mask))[:, ::-1]
    _, vectors = np.linalg.eigh(np.cov(points.T))
    direction = vectors[:, -1]
    angle = (math.degrees(math.atan2(direction[1], direction[0])) + 90) % 180 - 90
    if abs(angle) > 12:
        return None
    rotated = crop.rotate(angle, Image.Resampling.BICUBIC, expand=True, fillcolor="white")
    mask = (np.asarray(ImageOps.grayscale(rotated)) < 50).astype("uint8")
    count, labels, stats, _ = cv2.connectedComponentsWithStats(mask, 8)
    components = [stat for stat in stats[1:] if stat[4] > 20]
    if len(components) < 4:
        return None
    median_height = float(np.median([stat[3] for stat in components]))
    removed = []
    for label in range(1, count):
        x, y, width, height, area = (int(value) for value in stats[label])
        if 0.35 * median_height < height < 0.8 * median_height:
            mask[labels == label] = 0
            removed.append([x, y, width, height])
    count, labels, stats, _ = cv2.connectedComponentsWithStats(mask, 8)
    components = sorted((tuple(int(value) for value in stat) for stat in stats[1:] if stat[4] > 20), key=lambda stat: stat[0])
    words = []
    for x, y, width, height, area in components:
        if not words or x - words[-1][2] > 0.2 * median_height:
            words.append([x, y, x + width, y + height])
        else:
            old = words[-1]
            words[-1] = [old[0], min(old[1], y), max(old[2], x + width), max(old[3], y + height)]
    readings = []
    def read_word(bounds, allowlist):
        x, y, right, bottom = bounds
        pixels = (255 * (1 - mask[max(0, y - 5):bottom + 5, max(0, x - 5):right + 5])).astype("uint8")
        result = reader.recognize(pixels, decoder="beamsearch", beamWidth=10, allowlist=allowlist,
                                  detail=1, paragraph=False)
        best = max(result, key=lambda item: item[2]) if result else (None, "", 0)
        readings.append({"bounds": bounds, "allowlist": allowlist, "text": str(best[1]), "confidence": float(best[2])})
        return str(best[1]).strip(), float(best[2])
    month_index = None
    month, month_confidence = "", 0
    for index, word in enumerate(words[:4]):
        text, confidence = read_word(word, "ABCDEFGHIJKLMNOPQRSTUVWXYZ")
        if re.fullmatch(MONTH, text, re.I) and confidence >= 0.85:
            month_index, month, month_confidence = index, text.upper(), confidence
            break
    if month_index is None or month_index + 1 >= len(words):
        return None
    day, day_confidence = read_word(words[month_index + 1], "0123456789")
    if not re.fullmatch(r"\d{1,2}", day) or not 1 <= int(day) <= 31 or day_confidence < 0.85:
        return None
    text = f"BY {month} {int(day)}"
    confidence = min(month_confidence, day_confidence)
    if month_index + 2 < len(words) and re.search(r"[AP]M[.!]?$", box["text"].strip(), re.I):
        clock, clock_confidence = read_word(words[-1], "0123456789APM:")
        clock = normalize_date_spacing(clock)
        if re.fullmatch(r"(?:1[0-2]|[1-9])(?::[0-5]\d)?[AP]M", clock, re.I) and clock_confidence >= 0.85:
            text += " AT " + clock
            confidence = min(confidence, clock_confidence)
    recovered = {"text": text, "confidence": confidence, "polygon": box["polygon"]}
    return recovered, {"pass": "critical_fields", "crop": region, "rotation_degrees": angle,
                       "removed_superscript_components": removed, "field_readings": readings, "boxes": [recovered]}


def improve_boxes(image, boxes, reader):
    """At most three ROI retries, or one full-image retry for empty/poor reads."""
    attempts = [{"pass": "original", "boxes": boxes}]
    weak = [box for box in boxes if critical_date_row(box["text"]) and
            (box["confidence"] < 0.9 or date_signature(box["text"]) is None)]
    poor = not boxes or sum(box["confidence"] for box in boxes) / len(boxes) < 0.5
    if not weak and not poor:
        return boxes, attempts
    if weak:
        recovered = _recover_date_line(image, weak[0], reader)
        if recovered:
            replacement, audit = recovered
            attempts.append(audit)
            prior_date, new_date = date_signature(weak[0]["text"]), date_signature(replacement["text"])
            if prior_date and prior_date != new_date and weak[0]["confidence"] >= 0.85:
                return [*boxes, replacement], attempts
            return [replacement if box is weak[0] else box for box in boxes], attempts
    width, height = image.size
    if weak:
        # Enclose the actual uncertain line rather than a hand-picked post crop.
        left, top, right, bottom = _bounds(weak[0])
        pad_x, pad_y = max(30, (right - left) * 0.08), max(24, (bottom - top) * 0.35)
        region = (max(0, int(left - pad_x)), max(0, int(top - pad_y)),
                  min(width, int(right + pad_x)), min(height, int(bottom + pad_y)))
        angles = (0, 6, -6)
    else:
        region, angles = (0, 0, width, height), (0,)
    crop = image.crop(region)
    scale = min(2.0, 2600 / max(crop.size))
    size = tuple(max(1, round(value * scale)) for value in crop.size)
    base = ImageOps.autocontrast(ImageOps.grayscale(crop)).resize(size, Image.Resampling.LANCZOS).convert("RGB")
    pool = list(boxes)
    for angle in angles:
        pixels = base.rotate(angle, resample=Image.Resampling.BICUBIC, expand=False, fillcolor="white")
        detections = reader.readtext(np.asarray(pixels), detail=1, paragraph=False,
                                     decoder="beamsearch", beamWidth=5, text_threshold=0.5,
                                     low_text=0.3, canvas_size=2600, mag_ratio=1.0)
        found = []
        radians = math.radians(angle)
        cx, cy = pixels.width / 2, pixels.height / 2
        for polygon, text, confidence in detections:
            if not str(text).strip():
                continue
            mapped = []
            for x, y in polygon:
                dx, dy = float(x) - cx, float(y) - cy
                original_x = (cx + math.cos(radians) * dx - math.sin(radians) * dy) / scale + region[0]
                original_y = (cy + math.sin(radians) * dx + math.cos(radians) * dy) / scale + region[1]
                mapped.append({"x": min(width, max(0, original_x)), "y": min(height, max(0, original_y))})
            found.append({"text": str(text), "confidence": float(confidence), "polygon": mapped})
        attempts.append({"pass": "critical_roi" if weak else "contrast_upscale", "crop": region,
                         "scale": scale, "rotation_degrees": angle, "boxes": found})
        pool.extend(found)
    # Reconcile spatially, never by averaging confidence across unrelated words.
    selected = list(boxes)
    for candidate in pool[len(boxes):]:
        overlapping = [index for index, old in enumerate(selected) if _overlap(old, candidate) > 0.45]
        if not overlapping:
            selected.append(candidate)
            continue
        index = max(overlapping, key=lambda index: _overlap(selected[index], candidate))
        old = selected[index]
        old_date, new_date = date_signature(old["text"]), date_signature(candidate["text"])
        if old_date and new_date and old_date != new_date and min(old["confidence"], candidate["confidence"]) >= 0.85:
            # Two reliable but conflicting readings remain explicit for review.
            selected.append(candidate)
        elif (new_date is not None, candidate["confidence"]) > (old_date is not None, old["confidence"]):
            if candidate["confidence"] >= 0.8 or not critical_row(old["text"]):
                selected[index] = candidate
    selected.sort(key=lambda box: (_bounds(box)[1], _bounds(box)[0]))
    return selected, attempts
