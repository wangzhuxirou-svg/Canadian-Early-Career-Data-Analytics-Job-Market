"""Conservative, evidence-preserving opportunity draft generation."""

import argparse
import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from config.settings import SOFT_SKILL_KEYWORDS, TECHNICAL_SKILLS_KEYWORDS
from src.opportunities.content import exclusion_reason
from src.opportunities.temporal import clock_range, deadline_clock, relative_deadline

ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = ROOT / "data" / "opportunities"


ACTIVITY_RULES = {
    "workshop": ("workshop", "bootcamp", "training session"),
    "competition": ("competition", "hackathon", "case competition", "datathon"),
    "talk": ("talk", "panel", "lecture", "guest speaker"),
    "networking": ("networking", "networking night", "mixer"),
    "social": ("social", "social night", "welcome back"),
    "recruitment": ("recruitment open", "applications open", "join our team", "hiring", "chance to apply", "submit your application", "associate applications", "brand ambassador"),
    "volunteering": ("volunteer", "volunteering"),
    "research": ("research opportunity", "research assistant", "research position"),
    "other": ("general meeting", "annual general meeting", "yearly general meeting"),
}
TOPIC_RULES = {
    "data_ai": (
        "data science", "data analytics", "machine learning",
        "artificial intelligence", " ai ", " ml ",
    ),
    "software": ("software", "programming", "coding", "computer science"),
    "finance": ("finance", "financial", "investment", "accounting"),
    "business": ("business", "entrepreneur", "consulting"),
    "arts_culture": ("arts", "culture", "creative", "design"),
    "community": ("community",),
}
SKILL_CATALOG = {**TECHNICAL_SKILLS_KEYWORDS, **SOFT_SKILL_KEYWORDS}
DATE_PATTERN = re.compile(
    r"\b(?:"
    r"(?P<month>Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)"
    r"\s+(?P<day>\d{1,2})(?:st|nd|rd|th)?[,]?\s+(?P<year>20\d{2})"
    r"|(?P<iso_year>20\d{2})-(?P<iso_month>\d{2})-(?P<iso_day>\d{2})"
    r")\b",
    re.IGNORECASE,
)
SHORT_DATE_PATTERN = re.compile(
    r"\b(?P<month>Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\.?\s+(?P<day>\d{1,2})(?:st|nd|rd|th)?\b", re.I
)
URL_PATTERN = re.compile(r"https?://[^\s<>\"')\]]+", re.IGNORECASE)
REGISTRATION_REQUEST_PATTERN = re.compile(
    r"\b(?:register now|register here|registration is open|sign up|apply now|"
    r"applications are open|submit (?:an? )?application|apply by)\b",
    re.IGNORECASE,
)
DEADLINE_LABELS = ("deadline", "due date", "apply by", "applications close", "application closes",
                   "registration closes", "registration close", "register by", "applications will close",
                   "applications close on", "applications closing", "closes on")
LOCATION_PATTERN = re.compile(
    r"(?:(?:location|venue|address)\s*:\s*|📍\s*(?:(?:location|venue|address)\s*:\s*)?)([^\n\r]+)", re.IGNORECASE
)
TITLE_PREFIXES = ("title:", "event:", "workshop:", "opportunity:")
OPPORTUNITY_TERMS = (
    "workshop", "bootcamp", "competition", "hackathon", "datathon",
    "register", "registration", "apply", "application", "volunteer",
    "research opportunity", "join us", "join our team", "event",
    "general meeting", "annual general meeting", "yearly general meeting",
)
MEETING_TITLE_PATTERN = re.compile(
    r"\b(?:(?:yearly|annual)\s+)?general\s+meeting\b",
    re.IGNORECASE,
)
EXCLUSION_TERMS = (
    "thank you for joining", "thanks for joining", "event recap",
    "highlights from", "congratulations to", "throwback to",
)
RULE_VERSION = "opportunity_draft_rules_v3"


def _contains_any(text: str, terms: tuple[str, ...]) -> bool:
    lowered = f" {' '.join(text.casefold().split())} "
    return any(term in lowered for term in terms)


def _source_parts(raw_post: dict, ocr_dir: Path) -> list[dict]:
    parts = []
    caption = raw_post.get("caption_raw")
    if raw_post.get("caption_status") == "available" and isinstance(caption, str):
        parts.append({"source_part": "caption", "media_position": None, "text": caption})

    for media in raw_post.get("media", []):
        if media.get("type") != "image" or media.get("download_status") != "downloaded":
            continue
        image_hash = media.get("sha256")
        if not image_hash:
            continue
        results = []
        for result_path in ocr_dir.glob("*.json"):
            result = json.loads(result_path.read_text(encoding="utf-8"))
            if result.get("image_hash") == image_hash:
                results.append(result)
        if not results:
            continue
        result = max(results, key=lambda item: item.get("processed_at", ""))
        if result.get("status") == "success" and result.get("text"):
            parts.append({
                "source_part": "image_ocr",
                "media_position": media["position"],
                "text": result["text"],
            })
    return parts


def _evidence(
    parts: list[dict],
    matcher,
    field: str,
    candidate_value,
    rule_id: str,
) -> list[dict]:
    evidence = []
    for part in parts:
        for line in part["text"].splitlines():
            if matcher(line):
                text = line.strip()
                if text:
                    evidence.append({
                        "field": field,
                        "candidate_value": candidate_value,
                        "evidence_text": text,
                        "source_part": part["source_part"],
                        "media_position": part["media_position"],
                        "rule_id": rule_id,
                    })
    return evidence


def _date_candidates(parts: list[dict], field_name: str, labels: tuple[str, ...], posted_at: str | None = None):
    candidates = []
    for part in parts:
        date_text = part["text"]
        if part["source_part"] == "image_ocr":
            from src.opportunities.ocr_quality import normalize_date_spacing
            date_text = normalize_date_spacing(date_text)
            date_text = re.sub(r"\b(Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\s*\n\s*(\d{1,2})\b", r"\1 \2", date_text, flags=re.I)
        lines = date_text.splitlines()
        for index, line in enumerate(lines):
            lowered = line.casefold()
            context = " ".join(lines[max(0, index - 2):index + 1]).casefold() if part["source_part"] == "image_ocr" else lowered
            standalone_by = bool(re.search(r"^\W*by\s+(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec|20\d{2})", lowered, re.I))
            deadline_line = standalone_by or any(marker in lowered for marker in DEADLINE_LABELS) or bool(re.search(
                r"\b(?:apply|register|registration|sign[ -]?up|applications?)\b.*\b(?:by|before|clos\w*|deadline)\b", lowered
            )) or bool(re.search(r"\b(?:registration|applications?)\s+closes?\b", context)) or bool(re.search(r"\b\d+ days? left\b.*(?:sign[ -]?up|apply|register)", lowered))
            if field_name == "start" and deadline_line:
                continue
            if field_name != "start" and not deadline_line:
                continue
            if field_name == "deadline" and posted_at and not SHORT_DATE_PATTERN.search(line) and not DATE_PATTERN.search(line):
                relative = relative_deadline(line, posted_at)
                if relative:
                    candidates.append((relative, part, line.strip(), "relative_to_posted_at"))
            if field_name == "start" and posted_at and re.search(r"join us|takes place|happening|starts?|held", line, re.I) and re.search(r"\b(?:today|tomorrow)\b", line, re.I) and not SHORT_DATE_PATTERN.search(line):
                relative = relative_deadline(line, posted_at)
                if relative:
                    candidates.append((relative, part, line.strip(), "relative_to_posted_at"))
            for match in DATE_PATTERN.finditer(line):
                try:
                    if match.group("iso_year"):
                        date_value = datetime(
                            int(match.group("iso_year")),
                            int(match.group("iso_month")),
                            int(match.group("iso_day")),
                        ).date().isoformat()
                    else:
                        month = datetime.strptime(match.group('month')[:3].title(), "%b").month
                        date_value = datetime(int(match.group('year')), month, int(match.group('day'))).date().isoformat()
                except ValueError:
                    continue
                candidates.append((date_value, part, line.strip(), match.group(0)))
            if posted_at:
                anchor = datetime.fromisoformat(posted_at.replace("Z", "+00:00")).date()
                full_spans = [match.span() for match in DATE_PATTERN.finditer(line)]
                for match in SHORT_DATE_PATTERN.finditer(line):
                    if any(start <= match.start() < end for start, end in full_spans):
                        continue
                    # Inference is anchored to publication, never the wall-clock
                    # year. It is explicitly flagged for human confirmation.
                    month = datetime.strptime(match.group("month")[:3].title(), "%b").month
                    dates = []
                    for year in (anchor.year - 1, anchor.year, anchor.year + 1):
                        try:
                            value = datetime(year, month, int(match.group("day"))).date()
                        except ValueError:
                            continue
                        if abs((value - anchor).days) <= 120:
                            dates.append(value)
                    if len(dates) == 1:
                        candidates.append((dates[0].isoformat(), part, line.strip(), match.group(0)))
    unique = {}
    # Prefer the explicit caption's event dates over pricing windows or
    # unrelated dates on secondary carousel slides.
    caption_candidates = [item for item in candidates if item[1]["source_part"] == "caption"]
    if caption_candidates:
        candidates = caption_candidates
    if field_name == "start":
        labeled = [item for item in candidates if re.search(r"(?:\bdate\s*:|📅|📆|final round|join.{0,25}\bon\b|takes place)", item[2], re.I)]
        if labeled:
            candidates = labeled
    for candidate in candidates:
        unique.setdefault(candidate[0], candidate)
    return list(unique.values())


def _url_candidates(parts: list[dict]) -> list[tuple[str, dict, str]]:
    candidates = []
    for part in parts:
        for match in URL_PATTERN.finditer(part["text"]):
            url = match.group(0).rstrip(".,;:!?")
            host = (urlparse(url).hostname or "").casefold()
            if host in {"instagram.com", "www.instagram.com"}:
                continue
            line = part["text"].splitlines()
            source = next((item.strip() for item in line if url in item), url)
            candidates.append((url, part, source))
    return candidates


def _classified_values(parts: list[dict], rules: dict[str, tuple[str, ...]]):
    results = {}
    for value, keywords in rules.items():
        hits = []
        for part in parts:
            if _contains_any(part["text"], keywords):
                hits.append({
                    "field": "",
                    "candidate_value": value,
                    "evidence_text": part["text"].strip(),
                    "source_part": part["source_part"],
                    "media_position": part["media_position"],
                    "rule_id": "",
                })
        if hits:
            results[value] = hits
    return results


def _skills(parts: list[dict]) -> dict[str, list[dict]]:
    results = {}
    for skill_id, keywords in SKILL_CATALOG.items():
        for part in parts:
            matching_lines = [
                line.strip()
                for line in part["text"].splitlines()
                if _contains_any(line, tuple(keywords))
            ]
            if matching_lines:
                results[skill_id] = [{
                    "field": "skill_ids",
                    "candidate_value": [skill_id],
                    "evidence_text": line,
                    "source_part": part["source_part"],
                    "media_position": part["media_position"],
                    "rule_id": f"skill.{skill_id}.keyword",
                } for line in matching_lines]
    return results


def _draft_id(post_id: str) -> str:
    return f"instagram_{post_id}"


def _excluded_draft(raw_post: dict, reason: str) -> dict:
    now = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    post_id = raw_post["source_post_id"]
    return {
        "draft_id": _draft_id(post_id),
        "opportunity": {
            "opportunity_id": f"club_{hashlib.sha256(post_id.encode()).hexdigest()[:16]}",
            "kind": "event",
            "title": "Excluded post",
            "organizer_id": raw_post["organizer_id"],
            "source_url": raw_post["source_url"],
            "source_post_id": post_id,
            "source_post_ids": [post_id],
            "description": raw_post.get("caption_raw"),
            "responsibilities": None,
            "time_commitment": None,
            "compensation_status": "not_stated",
            "ccr_status": "not_stated",
            "activity_type_ids": [],
            "topic_ids": [],
            "skill_ids": [],
            "beginner_level": "unknown",
            "eligibility_note": None,
            "format": "unknown",
            "location": None,
            "start_at": None,
            "end_at": None,
            "deadline_at": None,
            "timezone": "America/Toronto",
            "date_precision": "unknown",
            "start_date": None,
            "end_date": None,
            "deadline_date": None,
            "start_precision": "unknown",
            "end_precision": "unknown",
            "deadline_precision": "unknown",
            "start_date_text": None,
            "end_date_text": None,
            "deadline_date_text": None,
            "verified_at": now,
            "review_status": "draft",
            "classification_reasons": [RULE_VERSION, reason],
            "registration_url": None,
            "source_updated_at": None,
            "content_revision": 1,
            "change_pending": False,
        },
        "evidence": [],
        "missing_fields": [],
        "conflicts": [],
        "exclusion_reason": reason,
        "created_at": now,
    }


def build_draft(raw_post: dict, ocr_dir: Path | None = None) -> dict | None:
    ocr_dir = ocr_dir or DATA_DIR / "ocr"
    parts = _source_parts(raw_post, ocr_dir)
    combined_text = "\n".join(part["text"] for part in parts)
    folded_text = combined_text.casefold()
    caption_exclusion = exclusion_reason(raw_post.get("caption_raw"))
    if caption_exclusion:
        return _excluded_draft(raw_post, caption_exclusion)
    combined_exclusion = exclusion_reason(combined_text)
    if combined_exclusion == "internal_club_meeting":
        excluded = _excluded_draft(raw_post, combined_exclusion)
        match = MEETING_TITLE_PATTERN.search(" ".join(combined_text.split()))
        excluded["opportunity"]["title"] = match.group(0) if match else "Internal club meeting"
        return excluded
    if _contains_any(folded_text, EXCLUSION_TERMS) and not REGISTRATION_REQUEST_PATTERN.search(combined_text):
        return _excluded_draft(raw_post, "retrospective_or_congratulatory_content")
    if not _contains_any(folded_text, OPPORTUNITY_TERMS):
        return None

    first_line = next((line.strip() for line in combined_text.splitlines() if line.strip()), "")
    title = first_line
    title_part = parts[0]
    title_evidence_text = first_line
    meeting_title = None
    for part in parts:
        normalized_text = " ".join(part["text"].split())
        match = MEETING_TITLE_PATTERN.search(normalized_text)
        if match:
            meeting_title = match.group(0)
            title_part = part
            title_evidence_text = match.group(0)
            break
    if meeting_title:
        title = meeting_title
    for prefix in TITLE_PREFIXES:
        if title.casefold().startswith(prefix):
            title = title[len(prefix):].strip()
            break
    title_is_candidate = bool(title and len(title) <= 140 and not title.startswith("@"))
    if not title_is_candidate:
        for part in parts:
            match = re.search(
                r"\b(?:[A-Z][\w’'-]*[ \t]+){1,5}(?:Workshop|Bootcamp|Summit|Conference|Panel|Competition|Meeting)\b"
                r"|\b(?:exclusive )?tour of [^,!?\n]{1,95}", part["text"]
            )
            if match:
                title = match.group(0).strip()
                title_part = part
                title_evidence_text = title
                title_is_candidate = True
                break
    if not title_is_candidate:
        title = "Untitled opportunity (needs review)"
    # Named event identifiers make better titles than promotional first lines.
    if raw_post.get("caption_raw"):
        match = re.search(r"\b[A-Z]{3,12}\s+20\d{2}\b", raw_post["caption_raw"])
        if match and not re.search(r"ambassador|mentorship|hiring|associate", raw_post["caption_raw"], re.I):
            title = match.group(0)
            title_part = next(part for part in parts if part["source_part"] == "caption")
            title_evidence_text = title
            title_is_candidate = True

    is_ongoing = _contains_any(
        folded_text,
        ("applications open", "apply to join", "join our team", "research assistant position",
         "hiring", "chance to apply", "associate", "brand ambassador", "mentorship program"),
    )
    if re.search(r"give (?:a |an? )?(?:five|\d+)[ -]minute talk|speaker applications?|call for speakers", combined_text, re.I):
        is_ongoing = True
    kind = "ongoing_opportunity" if is_ongoing else "event"
    evidence = []

    if title_is_candidate:
        evidence.append({
            "field": "title",
            "candidate_value": title,
            "evidence_text": title_evidence_text,
            "source_part": title_part["source_part"],
            "media_position": title_part["media_position"],
            "rule_id": "title.meeting_heading" if meeting_title else "title.first_line",
        })

    description = raw_post["caption_raw"] if raw_post.get("caption_status") == "available" else None
    if description:
        evidence.append({
            "field": "description",
            "candidate_value": description,
            "evidence_text": description,
            "source_part": "caption",
            "media_position": None,
            "rule_id": "description.caption_verbatim",
        })
    kind_part = next(
        part for part in parts
        if _contains_any(part["text"], OPPORTUNITY_TERMS)
    )
    evidence.append({
        "field": "kind",
        "candidate_value": kind,
        "evidence_text": kind_part["text"].strip(),
        "source_part": kind_part["source_part"],
        "media_position": kind_part["media_position"],
        "rule_id": "kind.opportunity_keyword",
    })

    activity_values = _classified_values(parts, ACTIVITY_RULES)
    activity_ids = sorted(activity_values)
    for activity_id, hits in activity_values.items():
        evidence.extend({
            **item,
            "field": "activity_type_ids",
            "rule_id": f"activity.{activity_id}.keyword",
        } for item in hits)

    topic_values = _classified_values(parts, TOPIC_RULES)
    topic_ids = sorted(topic_values)
    for topic_id, hits in topic_values.items():
        evidence.extend({
            **item,
            "field": "topic_ids",
            "rule_id": f"topic.{topic_id}.keyword",
        } for item in hits)

    skill_values = _skills(parts)
    skill_ids = sorted(skill_values)
    for hits in skill_values.values():
        evidence.extend(hits)

    start_candidates = _date_candidates(parts, "start", ("date", "on"), raw_post.get("posted_at"))
    deadline_candidates = _date_candidates(
        parts, "deadline", DEADLINE_LABELS, raw_post.get("posted_at")
    )
    conflicts = []
    if len(start_candidates) > 1:
        conflicts.append("Multiple distinct event dates were found; start date left unset.")
        start_candidate = None
    else:
        start_candidate = start_candidates[0] if start_candidates else None
    if len(deadline_candidates) > 1:
        conflicts.append("Multiple distinct deadline dates were found; deadline left unset.")
        deadline_candidate = None
    else:
        deadline_candidate = deadline_candidates[0] if deadline_candidates else None

    for field, candidate in (
        ("start_date", start_candidate),
        ("deadline_date", deadline_candidate),
    ):
        if candidate:
            value, part, source_text, _date_text = candidate
            evidence.append({
                "field": field,
                "candidate_value": value,
                "evidence_text": source_text,
                "source_part": part["source_part"],
                "media_position": part["media_position"],
                "rule_id": f"date.{field}." + ("relative_to_posted_at" if _date_text == "relative_to_posted_at" else "explicit_full_date" if DATE_PATTERN.search(_date_text) else "year_from_posted_at"),
            })

    location_candidates = []
    for part in parts:
        for match in LOCATION_PATTERN.finditer(part["text"]):
            value = match.group(1).strip().rstrip(".,;")
            if value:
                location_candidates.append((value, part, match.group(0).strip()))
    unique_locations = {item[0].casefold(): item for item in location_candidates}
    location = None
    if len(unique_locations) == 1:
        location, part, source_text = next(iter(unique_locations.values()))
        evidence.append({
            "field": "location",
            "candidate_value": location,
            "evidence_text": source_text,
            "source_part": part["source_part"],
            "media_position": part["media_position"],
            "rule_id": "location.explicit_label",
        })
    elif len(unique_locations) > 1:
        conflicts.append("Different labeled locations were found; location left unset.")

    format_rules = {
        "online": ("online", "virtual", "zoom"),
        "in_person": ("in person", "on campus", "in-person"),
        "hybrid": ("hybrid",),
    }
    format_values = _classified_values(parts, format_rules)
    format_id = next(iter(format_values)) if len(format_values) == 1 else "unknown"
    if len(format_values) > 1:
        conflicts.append("Conflicting event formats were found.")
    for format_value, hits in format_values.items():
        evidence.extend({
            **item,
            "field": "format",
            "rule_id": f"format.{format_value}.keyword",
        } for item in hits)

    registration_url = None
    url_candidates = _url_candidates(parts)
    if len({item[0] for item in url_candidates}) == 1:
        registration_url, part, source_text = url_candidates[0]
        evidence.append({
            "field": "registration_url",
            "candidate_value": registration_url,
            "evidence_text": source_text,
            "source_part": part["source_part"],
            "media_position": part["media_position"],
            "rule_id": "registration_url.explicit_url",
        })
    elif len({item[0] for item in url_candidates}) > 1:
        conflicts.append("Multiple URLs were found; registration link left unset.")

    eligibility_terms = ("open to", "open exclusively to", "eligibility", "must be", "only for", "restricted to")
    eligibility_note = None
    eligibility_hits = []
    for part in parts:
        for line in part["text"].splitlines():
            if _contains_any(line, eligibility_terms):
                eligibility_note = line.strip()
                eligibility_hits.append((part, line.strip()))
    if len({item[1] for item in eligibility_hits}) == 1 and eligibility_hits:
        part, line = eligibility_hits[0]
        evidence.append({
            "field": "eligibility_note",
            "candidate_value": eligibility_note,
            "evidence_text": line,
            "source_part": part["source_part"],
            "media_position": part["media_position"],
            "rule_id": "eligibility.explicit_phrase",
        })
    elif len({item[1] for item in eligibility_hits}) > 1:
        eligibility_note = "\n".join(dict.fromkeys(item[1] for item in eligibility_hits))
        for part, line in eligibility_hits:
            evidence.append({"field": "eligibility_note", "candidate_value": eligibility_note,
                             "evidence_text": line, "source_part": part["source_part"],
                             "media_position": part["media_position"], "rule_id": "eligibility.explicit_statements"})

    beginner_level = "unknown"
    beginner_terms = ("beginner-friendly", "no experience required", "no prior experience needed")
    prerequisite_terms = ("prerequisite", "prior experience required", "experience required")
    if _contains_any(folded_text, beginner_terms):
        beginner_level = "beginner_friendly"
    elif _contains_any(folded_text, prerequisite_terms):
        beginner_level = "prerequisites_required"
    if beginner_level != "unknown":
        matching_line = next(
            line.strip() for line in combined_text.splitlines()
            if _contains_any(line, beginner_terms if beginner_level == "beginner_friendly" else prerequisite_terms)
        )
        part = next(part for part in parts if matching_line in part["text"])
        evidence.append({
            "field": "beginner_level",
            "candidate_value": beginner_level,
            "evidence_text": matching_line,
            "source_part": part["source_part"],
            "media_position": part["media_position"],
            "rule_id": f"beginner_level.explicit_phrase",
        })

    activity_values = sorted(activity_values)
    skills_values = sorted(skill_values)
    start_date = start_candidate[0] if start_candidate else None
    deadline_date = deadline_candidate[0] if deadline_candidate else None
    if start_date:
        start_date_text = start_candidate[2]
    else:
        start_date_text = None
    missing_fields = []
    if not title_is_candidate:
        missing_fields.append("title")
    if kind == "event" and start_date is None:
        missing_fields.append("start_date")
    registration_requested = bool(REGISTRATION_REQUEST_PATTERN.search(combined_text))
    if (
        registration_requested
        and registration_url is None
        and not _contains_any(folded_text, ("link in bio", "link in our bio"))
    ):
        missing_fields.append("registration_url")

    now = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    opportunity_id = f"club_{hashlib.sha256(raw_post['source_post_id'].encode()).hexdigest()[:16]}"
    opportunity = {
        "opportunity_id": opportunity_id,
        "kind": kind,
        "title": title,
        "organizer_id": raw_post["organizer_id"],
        "source_url": raw_post["source_url"],
        "source_post_id": raw_post["source_post_id"],
        "source_post_ids": [raw_post["source_post_id"]],
        "description": description,
        "responsibilities": None,
        "time_commitment": None,
        "compensation_status": "not_stated",
        "ccr_status": "not_stated",
        "activity_type_ids": activity_values,
        "topic_ids": topic_ids,
        "skill_ids": skills_values,
        "beginner_level": beginner_level,
        "eligibility_note": eligibility_note,
        "format": format_id,
        "location": location,
        "start_at": None,
        "end_at": None,
        "deadline_at": None,
        "timezone": "America/Toronto",
        "date_precision": "date" if start_date else "unknown",
        "start_date": start_date,
        "end_date": None,
        "deadline_date": deadline_date,
        "start_precision": "date" if start_date else "unknown",
        "end_precision": "unknown",
        "deadline_precision": "date" if deadline_date else "unknown",
        "start_date_text": start_date_text,
        "end_date_text": None,
        "deadline_date_text": deadline_candidate[2] if deadline_date else None,
        "verified_at": now,
        "review_status": "needs_review",
        "classification_reasons": [RULE_VERSION],
        "registration_url": registration_url,
        "source_updated_at": None,
        "content_revision": 1,
        "change_pending": False,
    }

    # Keep clocks attached to a known local calendar date. No invented midnight
    # is used for date-only records, and deadline clocks are parsed separately.
    if start_date:
        ranges = []
        starts = []
        clock_errors = []
        for part in parts:
            for line in part["text"].splitlines():
                if any(label in line.casefold() for label in DEADLINE_LABELS):
                    continue
                try:
                    parsed = clock_range(line, start_date)
                except ValueError as error:
                    clock_errors.append((part, str(error)))
                    continue
                if parsed:
                    ranges.append((parsed, part, line.strip()))
                elif re.search(r"\btime\s*:|⏰|🕒|🕑", line, re.I) or line.strip() == (start_date_text or "").strip():
                    try:
                        exact_start = deadline_clock(line, start_date)
                    except ValueError as error:
                        clock_errors.append((part, str(error)))
                    else:
                        if exact_start:
                            starts.append((exact_start, part, line.strip()))
        caption_ranges = [item for item in ranges if item[1]["source_part"] == "caption"]
        for part, error in clock_errors:
            if part["source_part"] == "caption" or not (caption_ranges or any(item[1]["source_part"] == "caption" for item in starts)):
                conflicts.append(f"Clock time needs confirmation: {error}")
        ranges = caption_ranges or ranges
        if len({item[0] for item in ranges}) == 1:
            (start_at, end_at), part, line = ranges[0]
            opportunity.update(start_at=start_at, end_at=end_at, end_date=start_date,
                               start_precision="datetime", end_precision="datetime", date_precision="datetime",
                               start_date_text=(start_date_text or "") + " | " + line, end_date_text=line)
            for field, value in (("start_at", start_at), ("end_at", end_at)):
                evidence.append({"field": field, "candidate_value": value, "evidence_text": line,
                                 "source_part": part["source_part"], "media_position": part["media_position"],
                                 "rule_id": "time.explicit_range_toronto"})
        elif len({item[0] for item in ranges}) > 1:
            conflicts.append("Multiple distinct event time ranges were found.")
        elif starts:
            caption_starts = [item for item in starts if item[1]["source_part"] == "caption"]
            starts = caption_starts or starts
            if len({item[0] for item in starts}) == 1:
                value, part, line = starts[0]
                opportunity.update(start_at=value, start_precision="datetime", date_precision="datetime",
                                   start_date_text=(start_date_text or "") + " | " + line)
                evidence.append({"field": "start_at", "candidate_value": value, "evidence_text": line,
                                 "source_part": part["source_part"], "media_position": part["media_position"],
                                 "rule_id": "time.explicit_start_toronto"})
            else:
                conflicts.append("Multiple distinct event start times were found.")
    if deadline_candidate:
        value, part, line, _ = deadline_candidate
        try:
            exact = deadline_clock(line, value)
        except ValueError as error:
            conflicts.append(f"Deadline time needs confirmation: {error}")
        else:
            if exact:
                opportunity.update(deadline_at=exact, deadline_precision="datetime")
                evidence.append({"field": "deadline_at", "candidate_value": exact, "evidence_text": line,
                                 "source_part": part["source_part"], "media_position": part["media_position"],
                                 "rule_id": "time.explicit_deadline_toronto"})

    return {
        "draft_id": _draft_id(raw_post["source_post_id"]),
        "opportunity": opportunity,
        "evidence": evidence,
        "missing_fields": missing_fields,
        "conflicts": conflicts,
        "exclusion_reason": None,
        "created_at": now,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate an offline opportunity draft.")
    parser.add_argument("--post-id", required=True, help="Collected Instagram post ID")
    arguments = parser.parse_args()
    post_path = DATA_DIR / f"{arguments.post_id}.json"
    raw_post = json.loads(post_path.read_text(encoding="utf-8"))
    from src.opportunities.triage import reprocess
    report = reprocess(DATA_DIR)
    print(json.dumps(next(item for item in report["records"] if item["source_post_id"] == raw_post["source_post_id"]), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
