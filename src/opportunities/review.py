"""Local CSV and HTML opportunity review workflow."""

import argparse
import csv
import html
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote, urlparse

from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry, Resource

from config.settings import SOFT_SKILL_KEYWORDS, TECHNICAL_SKILLS_KEYWORDS
from src.opportunities.drafts import source_is_current


ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = ROOT / "data" / "opportunities"
DRAFTS_DIR = DATA_DIR / "drafts"
REVIEW_DIR = DATA_DIR / "review"
OVERRIDES_DIR = DATA_DIR / "review_overrides"
SCHEMAS_DIR = ROOT / "contracts" / "schemas"
ORGANIZERS_PATH = ROOT / "resources" / "organizers.json"
OVERRIDE_FIELDS = (
    "kind",
    "title",
    "description",
    "responsibilities",
    "time_commitment",
    "compensation_status",
    "ccr_status",
    "activity_type_ids",
    "topic_ids",
    "skill_ids",
    "beginner_level",
    "eligibility_note",
    "format",
    "location",
    "start_at",
    "end_at",
    "deadline_at",
    "timezone",
    "date_precision",
    "start_date",
    "end_date",
    "deadline_date",
    "start_precision",
    "end_precision",
    "deadline_precision",
    "start_date_text",
    "end_date_text",
    "deadline_date_text",
    "registration_url",
)
BASE_COLUMNS = (
    "draft_id",
    "opportunity_id",
    "source_url",
    "caption_status",
    "caption_raw",
    "candidate_values_json",
    "evidence_json",
    "missing_fields_json",
    "conflicts_json",
    "content_revision",
    "review_status",
    "reviewer",
    "reviewed_at",
    "reason",
)
OVERRIDE_PREFIX = "override__"
FORMULA_PREFIXES = ("=", "+", "-", "@")


class ReviewImportError(ValueError):
    """Raised when a review import contains invalid rows."""

    def __init__(self, message: str, report_path: Path):
        super().__init__(message)
        self.report_path = report_path


def _schemas_and_registry() -> tuple[dict, Registry]:
    schemas = {
        path.name.removesuffix(".schema.json"): json.loads(
            path.read_text(encoding="utf-8")
        )
        for path in SCHEMAS_DIR.glob("*.schema.json")
    }
    registry = Registry().with_resources(
        (schema["$id"], Resource.from_contents(schema))
        for schema in schemas.values()
    )
    return schemas, registry


def _validator(schema_name: str) -> Draft202012Validator:
    schemas, registry = _schemas_and_registry()
    return Draft202012Validator(
        schemas[schema_name],
        registry=registry,
        format_checker=FormatChecker(),
    )


def _load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _load_reviewable_drafts(include_auto: bool = False) -> list[dict]:
    if not DRAFTS_DIR.exists():
        return []
    drafts = []
    for path in sorted(DRAFTS_DIR.glob("*.json")):
        draft = _load_json(path)
        saved_triage = DATA_DIR / "triage" / path.name
        if saved_triage.exists() and _load_json(saved_triage).get("decision") == "merged_duplicate":
            continue
        from src.opportunities.events import display_draft
        draft = display_draft(DATA_DIR, draft)
        if (
            draft.get("exclusion_reason") is None
            and draft.get("opportunity", {}).get("review_status") == "needs_review"
        ):
            opportunity = draft["opportunity"]
            if not source_is_current(DATA_DIR, opportunity):
                continue
            triage_path = DATA_DIR / "triage" / path.name
            if triage_path.exists():
                from src.opportunities.triage import assess
                raw_path = DATA_DIR / f"{opportunity['source_post_id']}.json"
                if not raw_path.exists():
                    continue
                decision = assess(draft, _load_json(raw_path), DATA_DIR)
                if decision["decision"] == "excluded" or (decision["decision"] == "auto_candidate" and not include_auto):
                    continue
            ledger_path = OVERRIDES_DIR / f"{opportunity['opportunity_id']}.json"
            if ledger_path.is_file():
                ledger = _load_json(ledger_path)
                if (
                    ledger.get("content_revision") == opportunity["content_revision"]
                    and ledger.get("review_status") in {"approved", "rejected"}
                ):
                    continue
            drafts.append(draft)
    return drafts


def _formula_safe(value: str) -> str:
    if value.lstrip().startswith(FORMULA_PREFIXES):
        return "'" + value
    return value


def _formula_unescape(value: str) -> str:
    if value.startswith("'") and value[1:].lstrip().startswith(FORMULA_PREFIXES):
        return value[1:]
    return value


def _csv_value(value) -> str:
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        text = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    else:
        text = str(value)
    return _formula_safe(text)


def _candidate_values(draft: dict) -> dict:
    opportunity = draft["opportunity"]
    return {
        field: opportunity[field]
        for field in OVERRIDE_FIELDS
        if field in opportunity
    }


def _safe_local_image(media: dict, output_dir: Path) -> tuple[str, str] | None:
    if media.get("type") != "image" or media.get("download_status") != "downloaded":
        return None
    local_path = media.get("local_path")
    if not local_path:
        return None
    resolved = (ROOT / local_path).resolve()
    try:
        resolved.relative_to(ROOT)
        relative = Path(os.path.relpath(resolved, output_dir)).as_posix()
    except ValueError:
        return None
    if not resolved.is_file():
        return None
    return quote(relative, safe="/.-_"), html.escape(relative)


def _preview_html(drafts: list[dict], output_dir: Path) -> str:
    cards = []
    raw_posts = {}
    ocr_results = {}
    for path in DATA_DIR.glob("*.json"):
        try:
            raw_post = _load_json(path)
        except (OSError, json.JSONDecodeError):
            continue
        if raw_post.get("source_post_id"):
            raw_posts[raw_post["source_post_id"]] = raw_post
    ocr_dir = DATA_DIR / "ocr"
    if ocr_dir.exists():
        for path in ocr_dir.glob("*.json"):
            result = _load_json(path)
            image_hash = result.get("image_hash")
            if not image_hash:
                continue
            previous = ocr_results.get(image_hash)
            if previous is None or result.get("processed_at", "") > previous.get("processed_at", ""):
                ocr_results[image_hash] = result

    for draft in drafts:
        opportunity = draft["opportunity"]
        source_id = opportunity["source_post_id"]
        raw_post = raw_posts.get(source_id, {})
        title = html.escape(opportunity["title"])
        parsed_url = urlparse(opportunity["source_url"])
        if (
            parsed_url.scheme == "https"
            and parsed_url.netloc.casefold() == "www.instagram.com"
        ):
            source_link = '<a href="{}" rel="noreferrer">Open source post</a>'.format(
                html.escape(opportunity["source_url"], quote=True)
            )
        else:
            source_link = "Source URL is not a trusted Instagram link."
        source_ids = opportunity.get("source_post_ids", [source_id])
        caption = html.escape("\n\n".join(
            f"[{post_id}]\n" + (raw_posts.get(post_id, {}).get("caption_raw") or "Caption not collected.")
            for post_id in source_ids))
        source_link += "<p>All source posts: " + " · ".join(
            f'<a href="https://www.instagram.com/p/{quote(post_id, safe="")}/" rel="noreferrer">{html.escape(post_id)}</a>'
            for post_id in source_ids) + "</p>"
        evidence_rows = []
        for item in draft["evidence"]:
            media_position = item["media_position"]
            origin = (
                "Caption"
                if item["source_part"] == "caption"
                else f"Image {media_position}"
            )
            evidence_rows.append(
                "<li><strong>{}</strong> — {} <small>({})</small></li>".format(
                    html.escape(item["field"]),
                    html.escape(item["evidence_text"]),
                    html.escape(origin),
                )
            )
        images = []
        for media in raw_post.get("media", []):
            local = _safe_local_image(media, output_dir)
            if local:
                src, alt = local
                ocr_result = ocr_results.get(media.get("sha256"))
                if ocr_result:
                    ocr_text = (
                        html.escape(ocr_result["text"])
                        if ocr_result.get("text")
                        else "No readable text."
                    )
                    ocr_summary = (
                        f"<p>OCR status: {html.escape(ocr_result['status'])}</p>"
                        f"<pre>{ocr_text}</pre>"
                    )
                else:
                    ocr_summary = "<p>OCR not run.</p>"
                images.append(
                    '<figure><img src="{}" alt="{}"><figcaption>Image {}</figcaption>{}</figure>'.format(
                        html.escape(src, quote=True),
                        html.escape(f"Post image {media['position']}", quote=True),
                        media["position"],
                        ocr_summary,
                    )
                )
        missing = html.escape(", ".join(draft["missing_fields"]) or "None")
        conflicts = html.escape("; ".join(draft["conflicts"]) or "None")
        triage_path = DATA_DIR / "triage" / f"{source_id}.json"
        triage = _load_json(triage_path) if triage_path.exists() else {}
        triage_reasons = html.escape("; ".join(triage.get("reasons", [])) or "Not evaluated")
        cards.append(
            f"""
            <article>
              <h2>{title}</h2>
              <p>{source_link}</p>
              <p><strong>Missing:</strong> {missing}</p>
              <p><strong>Conflicts:</strong> {conflicts}</p>
              <p><strong>Triage reasons:</strong> {triage_reasons}</p>
              <section><h3>Collected caption</h3><pre>{caption}</pre></section>
              <section><h3>Downloaded images</h3><div class="images">{''.join(images)}</div></section>
              <section><h3>Candidate evidence</h3><ul>{''.join(evidence_rows)}</ul></section>
            </article>
            """
        )
    return """<!doctype html>
<html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>Local opportunity review</title>
<style>
body{font:16px system-ui,sans-serif;max-width:1100px;margin:2rem auto;padding:0 1rem;color:#222}
article{border:1px solid #bbb;border-radius:8px;padding:1rem;margin:1.5rem 0}
pre{white-space:pre-wrap;overflow-wrap:anywhere;background:#f5f5f5;padding:1rem}
.images{display:flex;flex-wrap:wrap;gap:1rem}figure{margin:0;width:min(280px,100%)}
img{max-width:100%;height:auto}small{color:#555}
</style><body><h1>Local opportunity review</h1>
<p>Private preview. Drafts are not approved or publishable.</p>
""" + "".join(cards) + "</body></html>"


def export_review(output_dir: Path | None = None, include_auto: bool = False) -> tuple[Path, Path]:
    output_dir = output_dir or REVIEW_DIR
    output_dir.mkdir(parents=True, exist_ok=True)
    drafts = _load_reviewable_drafts(include_auto=include_auto)
    columns = [
        *BASE_COLUMNS,
        "triage_reasons_json",
        *(f"{OVERRIDE_PREFIX}{field}" for field in OVERRIDE_FIELDS),
    ]
    csv_path = output_dir / "review.csv"
    with csv_path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=columns, extrasaction="raise")
        writer.writeheader()
        for draft in drafts:
            opportunity = draft["opportunity"]
            raw_post_path = DATA_DIR / f"{opportunity['source_post_id']}.json"
            raw_post = _load_json(raw_post_path) if raw_post_path.is_file() else {}
            row = {
                "draft_id": draft["draft_id"],
                "opportunity_id": opportunity["opportunity_id"],
                "source_url": opportunity["source_url"],
                "caption_status": raw_post.get("caption_status", "unavailable"),
                "caption_raw": raw_post.get("caption_raw"),
                "candidate_values_json": _candidate_values(draft),
                "evidence_json": draft["evidence"],
                "missing_fields_json": draft["missing_fields"],
                "conflicts_json": draft["conflicts"],
                "content_revision": opportunity["content_revision"],
                "review_status": "needs_review",
                "reviewer": "",
                "reviewed_at": "",
                "reason": "",
                "triage_reasons_json": (_load_json(DATA_DIR / "triage" / f"{opportunity['source_post_id']}.json").get("reasons", [])
                                        if (DATA_DIR / "triage" / f"{opportunity['source_post_id']}.json").exists() else []),
            }
            row.update({f"{OVERRIDE_PREFIX}{field}": "" for field in OVERRIDE_FIELDS})
            writer.writerow({key: _csv_value(value) for key, value in row.items()})

    html_path = output_dir / "review.html"
    html_path.write_text(_preview_html(drafts, output_dir), encoding="utf-8")
    return csv_path, html_path


def _parse_override(field: str, value: str):
    value = _formula_unescape(value)
    if value == "":
        return _MISSING
    if field in {"activity_type_ids", "topic_ids", "skill_ids"}:
        parsed = json.loads(value)
        if not isinstance(parsed, list):
            raise ValueError(f"{field} must be a JSON array.")
        return parsed
    if value == "null":
        return None
    return value


class _Missing:
    pass


_MISSING = _Missing()


def _current_draft(opportunity_id: str) -> dict | None:
    for draft in _load_reviewable_drafts(include_auto=True):
        if draft["opportunity"]["opportunity_id"] == opportunity_id:
            return draft
    return None


def _validate_references(patch: dict) -> None:
    organizers = _load_json(ORGANIZERS_PATH)
    organizer_ids = {item["organizer_id"] for item in organizers}
    skills = set(TECHNICAL_SKILLS_KEYWORDS) | set(SOFT_SKILL_KEYWORDS)
    current = _current_draft(patch["opportunity_id"])
    if current and current["opportunity"]["organizer_id"] not in organizer_ids:
        raise ValueError("Draft references an unknown organizer.")
    active_overrides = _merged_overrides(patch)
    override_skills = active_overrides.get("skill_ids", current["opportunity"]["skill_ids"] if current else [])
    invalid_skills = sorted(set(override_skills) - skills)
    if invalid_skills:
        raise ValueError(f"Unknown skill IDs: {', '.join(invalid_skills)}.")
    if current:
        updated = dict(current["opportunity"])
        updated.update(active_overrides)
        updated["review_status"] = patch["review_status"]
        validation_errors = sorted(
            _validator("opportunity").iter_errors(updated),
            key=lambda error: list(error.absolute_path),
        )
        if validation_errors:
            message = "; ".join(error.message for error in validation_errors)
            raise ValueError(f"Updated opportunity contract invalid: {message}")
        _validate_date_order(updated)


def _merged_overrides(patch: dict) -> dict:
    path = OVERRIDES_DIR / f"{patch['opportunity_id']}.json"
    active = {}
    if path.exists():
        previous = _load_json(path)
        if previous.get("content_revision") == patch["content_revision"]:
            active.update(previous.get("field_overrides", {}))
    active.update(patch["field_overrides"])
    return active


def _validate_date_order(opportunity: dict) -> None:
    for start_key, end_key in (("start_date", "end_date"), ("start_at", "end_at")):
        start, end = opportunity.get(start_key), opportunity.get(end_key)
        if start and end and start_key.endswith("_at"):
            start = datetime.fromisoformat(start.replace("Z", "+00:00"))
            end = datetime.fromisoformat(end.replace("Z", "+00:00"))
        if start and end and end < start:
            raise ValueError("Updated opportunity end must not precede its start.")


def effective_approved_opportunity(draft: dict, data_dir: Path | None = None) -> dict | None:
    """D7 handoff: return only the current, explicitly approved effective record."""
    opportunity = draft["opportunity"]
    source_dir = data_dir or DATA_DIR
    overrides_dir = source_dir / "review_overrides" if data_dir else OVERRIDES_DIR
    if not (source_dir / "draft_versions" / f"{opportunity['source_post_id']}.json").is_file():
        return None
    if draft.get("exclusion_reason") or not source_is_current(source_dir, opportunity):
        return None
    path = overrides_dir / f"{opportunity['opportunity_id']}.json"
    if not path.exists():
        return None
    ledger = _load_json(path)
    if ledger.get("content_revision") != opportunity["content_revision"] or ledger.get("review_status") != "approved":
        return None
    unknown_fields = set(ledger["field_overrides"]) - set(OVERRIDE_FIELDS)
    if unknown_fields:
        raise ValueError("Review ledger contains unsupported override fields.")
    # Validate the actual effective record even when a ledger was edited manually.
    updated = {**opportunity, **ledger["field_overrides"], "review_status": "approved",
               "change_pending": False, "verified_at": ledger["updated_at"]}
    _validator("opportunity").validate(updated)
    _validate_date_order(updated)
    organizers = {item["organizer_id"] for item in _load_json(ORGANIZERS_PATH)}
    skills = set(TECHNICAL_SKILLS_KEYWORDS) | set(SOFT_SKILL_KEYWORDS)
    if updated["organizer_id"] not in organizers or set(updated["skill_ids"]) - skills:
        raise ValueError("Effective opportunity has unknown organizer or skill references.")
    return updated


def effective_publishable_opportunity(draft: dict) -> dict | None:
    """D7 candidate reader: recheck time/source and preserve human priority."""
    from src.opportunities.triage import assess, candidate_projection
    from src.opportunities.events import display_draft
    triage_path = DATA_DIR / "triage" / f"{draft['opportunity']['source_post_id']}.json"
    if triage_path.exists() and _load_json(triage_path).get("decision") == "merged_duplicate":
        return None
    draft = display_draft(DATA_DIR, draft)
    raw_path = DATA_DIR / f"{draft['opportunity']['source_post_id']}.json"
    if not raw_path.exists() or not source_is_current(DATA_DIR, draft["opportunity"]):
        return None
    raw = _load_json(raw_path)
    approved = effective_approved_opportunity(draft)
    effective_draft = {**draft, "opportunity": approved or draft["opportunity"]}
    decision = assess(effective_draft, raw, DATA_DIR)
    if decision["decision"] == "excluded":
        return None
    if set(decision["reasons"]) & {"unknown_or_disabled_organizer", "source_version_not_current",
                                    "source_url_invalid", "raw_post_contract_invalid", "opportunity_contract_invalid"}:
        return None
    if approved:
        return {"opportunity": approved, "eligibility": {**decision, "decision": "human_approved", "reasons": ["current_human_approval"]}}
    if decision["decision"] == "auto_candidate":
        opportunity = candidate_projection(draft)
        _validator("opportunity").validate(opportunity)
        return {"opportunity": opportunity, "eligibility": decision}
    return None


def _atomic_write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{path.stem}.", suffix=".tmp", dir=path.parent
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        Path(temporary_name).replace(path)
    except BaseException:
        Path(temporary_name).unlink(missing_ok=True)
        raise


def _apply_patch(patch: dict) -> Path:
    path = OVERRIDES_DIR / f"{patch['opportunity_id']}.json"
    history = []
    active_overrides = _merged_overrides(patch)
    if path.exists():
        previous = _load_json(path)
        history = previous.get("history", [])
    record = {
        "opportunity_id": patch["opportunity_id"],
        "content_revision": patch["content_revision"],
        "review_status": patch["review_status"],
        "field_overrides": active_overrides,
        "updated_by": patch["reviewer"],
        "updated_at": patch["reviewed_at"],
        "reason": patch["reason"],
        "history": [*history, patch],
    }
    _atomic_write_json(path, record)
    return path


def import_review(csv_path: Path, report_path: Path | None = None) -> dict:
    report_path = report_path or csv_path.with_name("review-import-report.json")
    errors = []
    applied = []
    validator = _validator("review-patch")
    with csv_path.open("r", encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        headers = set(reader.fieldnames or [])
        required = set(BASE_COLUMNS)
        expected_overrides = {f"{OVERRIDE_PREFIX}{field}" for field in OVERRIDE_FIELDS}
        missing_headers = sorted(required - headers)
        unknown_override_headers = sorted(
            header for header in headers
            if header.startswith(OVERRIDE_PREFIX) and header not in expected_overrides
        )
        if missing_headers or unknown_override_headers:
            errors.append({
                "row": 1,
                "opportunity_id": None,
                "errors": [
                    *([f"Missing required columns: {', '.join(missing_headers)}"] if missing_headers else []),
                    *([f"Unknown override columns: {', '.join(unknown_override_headers)}"] if unknown_override_headers else []),
                ],
            })
        else:
            seen_opportunity_ids = set()
            for row_number, row in enumerate(reader, start=2):
                if not any((value or "").strip() for value in row.values()):
                    continue
                opportunity_id = (row.get("opportunity_id") or "").strip()
                try:
                    current = _current_draft(opportunity_id)
                    if current is None:
                        raise ValueError("No current reviewable draft matches this opportunity ID.")
                    current_opportunity = current["opportunity"]
                    if opportunity_id in seen_opportunity_ids:
                        raise ValueError("Duplicate opportunity ID in review CSV.")
                    seen_opportunity_ids.add(opportunity_id)
                    if row["draft_id"] != current["draft_id"]:
                        raise ValueError("Draft ID does not match the current opportunity.")
                    if row["source_url"] != current_opportunity["source_url"]:
                        raise ValueError("Source URL does not match the current draft.")
                    revision = int(row["content_revision"])
                    if revision != current_opportunity["content_revision"]:
                        raise ValueError(
                            f"Stale review revision {revision}; current revision is "
                            f"{current_opportunity['content_revision']}."
                        )
                    overrides = {}
                    for field in OVERRIDE_FIELDS:
                        column = f"{OVERRIDE_PREFIX}{field}"
                        if column in row:
                            value = _parse_override(field, row.get(column) or "")
                            if value is not _MISSING:
                                overrides[field] = value
                    status = (row.get("review_status") or "").strip()
                    patch = {
                        "opportunity_id": opportunity_id,
                        "content_revision": revision,
                        "review_status": status,
                        "reviewer": _formula_unescape((row.get("reviewer") or "").strip()),
                        "reviewed_at": (row.get("reviewed_at") or "").strip(),
                        "reason": _formula_unescape((row.get("reason") or "").strip()),
                        "field_overrides": overrides,
                    }
                    validation_errors = sorted(
                        validator.iter_errors(patch),
                        key=lambda error: list(error.absolute_path),
                    )
                    if validation_errors:
                        message = "; ".join(error.message for error in validation_errors)
                        raise ValueError(f"Review patch contract invalid: {message}")
                    _validate_references(patch)
                    applied_path = _apply_patch(patch)
                    applied.append({
                        "row": row_number,
                        "opportunity_id": opportunity_id,
                        "path": str(applied_path),
                    })
                except (ValueError, KeyError, json.JSONDecodeError) as error:
                    errors.append({
                        "row": row_number,
                        "opportunity_id": opportunity_id or None,
                        "errors": [str(error)],
                    })

    report = {
        "imported_at": datetime.now(timezone.utc).isoformat(
            timespec="seconds"
        ).replace("+00:00", "Z"),
        "source_csv": str(csv_path),
        "applied": applied,
        "errors": errors,
    }
    _atomic_write_json(report_path, report)
    if errors:
        raise ReviewImportError(
            f"Review import applied {len(applied)} row(s) and rejected "
            f"{len(errors)} row(s); see {report_path}",
            report_path,
        )
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description="Export or import local opportunity reviews.")
    subparsers = parser.add_subparsers(dest="command", required=True)
    export_parser = subparsers.add_parser("export", help="Write review CSV and HTML preview.")
    export_parser.add_argument("--output-dir", type=Path)
    export_parser.add_argument("--include-auto", action="store_true", help="Also include automatic candidates for optional spot checks.")
    import_parser = subparsers.add_parser("import", help="Validate and apply a reviewed CSV.")
    import_parser.add_argument("--csv", type=Path, required=True)
    import_parser.add_argument("--report", type=Path)
    arguments = parser.parse_args()

    if arguments.command == "export":
        csv_path, html_path = export_review(arguments.output_dir, include_auto=arguments.include_auto)
        print(f"Review CSV: {csv_path}\nLocal HTML preview: {html_path}")
    else:
        try:
            report = import_review(arguments.csv, arguments.report)
        except ReviewImportError as error:
            parser.exit(1, f"{error}\n")
        print(f"Applied {len(report['applied'])} review patch(es).")


if __name__ == "__main__":
    main()
