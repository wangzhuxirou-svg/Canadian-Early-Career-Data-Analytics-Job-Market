"""Revision-aware draft storage. Review ledgers are never overwritten here."""

import copy
import json
import os
import tempfile
from pathlib import Path


def has_usable_evidence(post: dict, data_dir: Path | None = None) -> bool:
    """Missing video is a coverage limitation, not missing caption evidence."""
    usable = post.get("collection_status") == "complete" or bool(
        post.get("collection_status") == "partial" and post.get("caption_status") == "available"
        and (post.get("caption_raw") or "").strip()
    )
    if not usable and post.get("collection_status") == "partial" and data_dir is not None:
        from src.opportunities.extract import _source_parts
        usable = any(part["text"].strip() for part in _source_parts(post, data_dir / "ocr"))
    return usable


def atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{path.stem}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        Path(name).replace(path)
    finally:
        Path(name).unlink(missing_ok=True)


def track_source(data_dir: Path, post: dict, revision: int | None = None) -> dict:
    """Invalidate approval immediately, including when later OCR/extraction fails."""
    path = data_dir / "draft_versions" / f"{post['source_post_id']}.json"
    previous = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    draft_path = data_dir / "drafts" / f"{post['source_post_id']}.json"
    old = json.loads(draft_path.read_text(encoding="utf-8")) if draft_path.exists() else None
    old_revision = old["opportunity"]["content_revision"] if old else 1
    changed = bool(previous and previous.get("content_hash") != post["content_hash"])
    number = max(revision or 1, previous.get("content_revision", old_revision) + int(changed))
    if not previous:
        # Legacy drafts have no source hash: require a fresh review rather than
        # silently retaining a possibly stale approval.
        number = max(number, old_revision + int(old is not None))
    record = {
        "source_post_id": post["source_post_id"],
        "content_hash": post["content_hash"],
        "content_revision": number,
        "ready": False,
        "reason": "awaiting_extraction" if post["collection_status"] == "complete" else "incomplete_snapshot",
    }
    if previous.get("content_hash") == post["content_hash"]:
        record["processing_issues"] = previous.get("processing_issues", [])
    if previous.get("content_hash") == post["content_hash"] and previous.get("ready") and has_usable_evidence(post, data_dir):
        record = previous
    atomic_json(path, record)
    return record


def store_draft(data_dir: Path, post: dict, draft: dict | None, version: dict,
                reason: str | None = None) -> str:
    path = data_dir / "drafts" / f"{post['source_post_id']}.json"
    previous = json.loads(path.read_text(encoding="utf-8")) if path.exists() else None
    metadata = dict(version)
    if reason or draft is None or not has_usable_evidence(post, data_dir):
        metadata.update(ready=False, reason=reason or "no_opportunity_evidence")
        atomic_json(data_dir / "draft_versions" / path.name, metadata)
        return metadata["reason"]
    if previous and previous["opportunity"]["content_revision"] == version["content_revision"]:
        def comparable(value):
            value = copy.deepcopy(value)
            value.pop("created_at", None)
            for key in ("verified_at", "content_revision", "change_pending", "review_status"):
                value["opportunity"].pop(key, None)
            return value
        if comparable(previous) == comparable(draft):
            metadata.update(ready=not bool(draft.get("exclusion_reason")), reason=draft.get("exclusion_reason"))
            atomic_json(data_dir / "draft_versions" / path.name, metadata)
            return "existing_draft_preserved"
        # Changed OCR/rules can change candidates without changing source bytes.
        metadata["content_revision"] = version["content_revision"] + 1
    if previous:
        archive = data_dir / "draft_history" / path.stem / f"{previous['opportunity']['content_revision']}.json"
        if not archive.exists():
            atomic_json(archive, previous)
    updated = copy.deepcopy(draft)
    updated["opportunity"].update(
        content_revision=metadata["content_revision"],
        change_pending=previous is not None,
        review_status="needs_review",
    )
    atomic_json(path, updated)
    metadata.update(ready=not bool(updated.get("exclusion_reason")), reason=updated.get("exclusion_reason"))
    atomic_json(data_dir / "draft_versions" / path.name, metadata)
    return "needs_review" if metadata["ready"] else "excluded"


def source_is_current(data_dir: Path, opportunity: dict) -> bool:
    """Guard for review and the eventual D7 publisher; support legacy drafts."""
    post_id = opportunity["source_post_id"]
    if len(opportunity.get("source_post_ids", [])) > 1:
        group_path = data_dir / "event_groups" / f"{post_id}.json"
        if not group_path.exists():
            return False
        group = json.loads(group_path.read_text(encoding="utf-8"))
        if not group.get("active", True) or group["content_revision"] != opportunity["content_revision"]:
            return False
        return all(source_is_current(data_dir, {"source_post_id": item["source_post_id"],
                                               "content_revision": item["content_revision"]})
                   for item in group["members"])
    metadata_path = data_dir / "draft_versions" / f"{post_id}.json"
    raw_path = data_dir / f"{post_id}.json"
    if not metadata_path.exists():
        return not raw_path.exists() or json.loads(raw_path.read_text(encoding="utf-8")).get("collection_status") == "complete"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    if not metadata.get("ready") or metadata["content_revision"] != opportunity["content_revision"]:
        return False
    if not raw_path.exists():
        return False
    raw = json.loads(raw_path.read_text(encoding="utf-8"))
    return has_usable_evidence(raw, data_dir) and raw.get("content_hash") == metadata["content_hash"]
