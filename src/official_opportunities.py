"""Load reviewed public listings for the production D7 package."""

import json
from datetime import datetime
from pathlib import Path
import re
from urllib.parse import urlsplit


def _utc(value: str) -> datetime:
    if not isinstance(value, str) or not re.fullmatch(
        r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?Z", value
    ):
        raise ValueError("Official opportunity dates must be UTC instants.")
    return datetime.fromisoformat(value)


def load_official_opportunities(import_path: Path) -> tuple[list[dict], list[dict], list[dict]]:
    """Return organizers, reviewed opportunities, and their observation records."""
    document = json.loads(Path(import_path).read_text(encoding="utf-8"))
    if (
        not isinstance(document, dict)
        or set(document) != {"organizers", "opportunities"}
        or not isinstance(document["organizers"], list)
        or not isinstance(document["opportunities"], list)
    ):
        raise ValueError("Official opportunity import must contain organizers and opportunities arrays.")

    opportunities, observations = [], []
    for item in document["opportunities"]:
        if not isinstance(item, dict) or set(item) != {"opportunity", "observed_at", "review_ref"}:
            raise ValueError("Each official opportunity must include its observation and approval reference.")
        record = item["opportunity"]
        review_ref = item["review_ref"]
        if (
            not isinstance(record, dict)
            or record.get("review_status") != "approved"
            or record.get("change_pending") is not False
            or not isinstance(review_ref, str)
            or not review_ref.strip()
        ):
            raise ValueError("Each official opportunity requires a current human approval.")
        source_url = record.get("source_url")
        source = urlsplit(source_url if isinstance(source_url, str) else "")
        if (
            source.scheme != "https"
            or not source.hostname
            or source.username is not None
            or source.password is not None
            or source.hostname == "instagram.com"
            or source.hostname.endswith(".instagram.com")
        ):
            raise ValueError("Official opportunities require an official public HTTPS source.")
        opportunity_id = record.get("opportunity_id")
        if not isinstance(opportunity_id, str) or not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,127}", opportunity_id):
            raise ValueError("Official opportunities require a stable occurrence ID.")
        observed_at = item["observed_at"]
        verified_at = record.get("verified_at")
        if _utc(observed_at) > _utc(verified_at):
            raise ValueError("The official opportunity was observed after its approval.")

        source_id = f"official:{opportunity_id}"
        if (
            record.get("source_post_id") not in (None, source_id)
            or record.get("source_post_ids") not in (None, [], [source_id])
        ):
            raise ValueError("Official opportunities cannot reuse another source's occurrence IDs.")
        opportunities.append({**record, "source_post_id": source_id, "source_post_ids": [source_id]})
        observations.append({
            "opportunity_id": opportunity_id,
            "source": "official",
            "source_post_ids": [{
                "source_post_id": source_id,
                "first_seen_at": observed_at,
                "last_seen_at": observed_at,
                "last_fetched_at": observed_at,
            }],
            "eligibility": {
                "decision": "human_approved",
                "content_revision": record.get("content_revision"),
                "evaluated_at": verified_at,
                "reasons": ["official_source_human_review"],
                "warnings": [],
            },
        })
    return document["organizers"], opportunities, observations
