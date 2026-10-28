"""Conservative same-organizer event identity and reversible multi-post views."""

import copy
import hashlib
import json
import re
from pathlib import Path

from src.opportunities.drafts import atomic_json


def identity_keys(draft: dict, raw: dict) -> set[str]:
    text = raw.get("caption_raw") or draft["opportunity"]["title"]
    text += " " + " ".join(re.sub(r"(?<=[a-z])(?=[A-Z])", " ", tag) for tag in re.findall(r"#([A-Za-z][A-Za-z0-9]+)", text))
    acronyms = [(name, year) for name, year in re.findall(r"\b([A-Z]{3,12})\s*(20\d{2})\b", text)
                if name not in {"UTSC", "UOFT", "JANUARY", "FEBRUARY", "MARCH", "APRIL", "MAY", "JUNE", "JULY", "AUGUST", "SEPTEMBER", "OCTOBER", "NOVEMBER", "DECEMBER", "SUMMER", "WINTER", "FALL", "SPRING"}]
    if acronyms:
        return {f"named:{name.casefold()}:{year}" for name, year in acronyms}
    keys = set()
    for match in re.finditer(r"\b([A-Z]{2,8})[’']?s\b.{0,25}\bmentorship program\b", text, re.I):
        keys.add("named:" + match.group(1).lower() + " mentorship program")
    for match in re.finditer(r"\b(?:[A-Z][\w’'-]*\s+(?:(?:in|of|the|and|for)\s+)?){1,5}(?:Summit|Conference|Competition|Bootcamp|Workshop|Mentorship Program|Sports Day|General Meeting)\b", text):
        tokens = re.findall(r"[a-z0-9]+", match.group(0).lower())
        tokens = [token for token in tokens if token not in {"the", "in", "of", "and", "for", "our"}]
        for index, token in enumerate(tokens):
            if token in {"summit", "conference", "bootcamp", "workshop"} and index >= 1:
                keys.add("named:" + " ".join(tokens[max(0, index - 1):index + 1]))
        # "Case Competition" and "General Meeting" are categories, not IDs.
        generic = {"case", "competition", "general", "meeting", "workshop", "annual", "yearly"}
        for size in (2, 3, 4):
            tail = tokens[-size:]
            if len(tail) == size and any(token not in generic for token in tail[:-1]):
                keys.add("named:" + " ".join(tail))
    return keys


def compatible(group, item):
    draft, raw = item
    opportunity = draft["opportunity"]
    for previous, prior_raw in group:
        prior = previous["opportunity"]
        if prior["organizer_id"] != opportunity["organizer_id"] or prior["kind"] != opportunity["kind"]:
            return False
        if prior.get("start_date") and opportunity.get("start_date") and prior["start_date"] != opportunity["start_date"]:
            return False
        if not identity_keys(previous, prior_raw) & identity_keys(draft, raw):
            return False
        years = lambda text: set(re.findall(r"\b20\d{2}\b", text or ""))
        old_years, new_years = years(prior_raw.get("caption_raw")), years(raw.get("caption_raw"))
        if old_years and new_years and not old_years & new_years:
            return False
        # Preserve separate recurring sessions even when a common series name matches.
        session = lambda text: set(re.findall(r"\b(?:week|session|part|day)\s*(\d+)\b", text or "", re.I))
        a, b = session(prior_raw.get("caption_raw")), session(raw.get("caption_raw"))
        if a and b and a != b:
            return False
        if a != b and (a or b) and not (prior.get("start_date") and prior["start_date"] == opportunity.get("start_date")):
            return False
    return True


def merge_group(data_dir: Path, members: list[tuple[dict, dict]]) -> dict:
    """Source drafts stay intact; a group has its own revision and provenance."""
    members = sorted(members, key=lambda item: (item[1].get("posted_at") or "", item[1]["source_post_id"]))
    base, raw = members[0]
    member_ids = {source["source_post_id"] for _, source in members}
    previous_groups = []
    for previous_path in (data_dir / "event_groups").glob("*.json"):
        previous = json.loads(previous_path.read_text(encoding="utf-8"))
        overlap = len(member_ids & {member["source_post_id"] for member in previous["members"]})
        if previous["canonical_post_id"] in member_ids and overlap >= 2:
            previous_groups.append((overlap, previous["canonical_post_id"]))
    if previous_groups:
        preferred = sorted(previous_groups, key=lambda item: (-item[0], item[1]))[0][1]
        base, raw = next(item for item in members if item[1]["source_post_id"] == preferred)
    leader = raw["source_post_id"]
    merged = copy.deepcopy(base)
    opportunity = merged["opportunity"]
    ids = [leader] + [item[1]["source_post_id"] for item in members if item[1]["source_post_id"] != leader]
    opportunity["source_post_ids"] = ids
    provenance = {}
    updates = []
    # All evidence remains available, with post IDs in the private provenance.
    merged["evidence"] = [copy.deepcopy(evidence) for draft, _ in members for evidence in draft["evidence"]]
    merged["conflicts"] = sorted(set(conflict for draft, _ in members for conflict in draft["conflicts"]))
    for field in ("title", "description", "location", "registration_url", "eligibility_note",
                  "start_date", "end_date", "deadline_date", "start_at", "end_at", "deadline_at"):
        choices = [(draft["opportunity"].get(field), source, draft) for draft, source in members
                   if draft["opportunity"].get(field)]
        if not choices:
            continue
        if field == "title":
            selected = next((item for item in choices if re.fullmatch(r"[A-Z]{3,12}\s*20\d{2}", item[0])), choices[0])
        elif field == "description":
            selected = max(choices, key=lambda item: len(item[0]))
        else:
            selected = choices[-1]
        unique = {str(item[0]).casefold().replace("’", "'") for item in choices}
        if field in {"start_at", "end_at", "deadline_at", "deadline_date"} and len(unique) > 1:
            # Date extensions need a human when posts disagree; never invent a
            # single deadline by choosing whichever is most convenient.
            latest_value = choices[-1][0]
            confirmations = sum(item[0] == latest_value for item in choices)
            update_text = choices[-1][1].get("caption_raw") or ""
            if field.startswith("deadline") and (confirmations >= 2 or re.search(r"extend|reopen|more spots|last call|new deadline|now closes", update_text, re.I)):
                updates.append(f"{field}: later update supersedes {sorted(unique)}")
            else:
                merged["conflicts"].append(f"Related posts disagree on {field}.")
                opportunity[field] = None
                continue
        opportunity[field] = selected[0]
        provenance[field] = selected[1]["source_post_id"]
        if field.endswith("_date") or field.endswith("_at"):
            prefix = field.rsplit("_", 1)[0]
            for suffix in ("precision", "date_text"):
                opportunity[f"{prefix}_{suffix}"] = selected[2]["opportunity"][f"{prefix}_{suffix}"]
    for prefix in ("start", "end", "deadline"):
        if any(f"Related posts disagree on {prefix}_date." == conflict for conflict in merged["conflicts"]):
            opportunity[f"{prefix}_at"] = None
            opportunity[f"{prefix}_date"] = None
        elif any(f"Related posts disagree on {prefix}_at." == conflict for conflict in merged["conflicts"]):
            opportunity[f"{prefix}_at"] = None
        if opportunity.get(f"{prefix}_at"):
            opportunity[f"{prefix}_precision"] = "datetime"
        elif opportunity.get(f"{prefix}_date"):
            opportunity[f"{prefix}_precision"] = "date"
        else:
            opportunity[f"{prefix}_precision"] = "unknown"
    opportunity["date_precision"] = opportunity["start_precision"]
    merged["missing_fields"] = [field for field in base["missing_fields"] if not opportunity.get(field)]
    for field in ("skill_ids", "activity_type_ids", "topic_ids"):
        opportunity[field] = sorted({value for draft, _ in members for value in draft["opportunity"][field]})
    fingerprint_members = [{"source_post_id": source["source_post_id"], "content_hash": source["content_hash"],
                            "content_revision": draft["opportunity"]["content_revision"]} for draft, source in members]
    fingerprint = hashlib.sha256(json.dumps({"members": fingerprint_members,
        "opportunity": {key: value for key, value in opportunity.items() if key not in {"verified_at", "content_revision", "change_pending"}},
        "conflicts": merged["conflicts"], "evidence": merged["evidence"]}, sort_keys=True).encode()).hexdigest()
    path = data_dir / "event_groups" / f"{leader}.json"
    old = json.loads(path.read_text(encoding="utf-8")) if path.exists() else None
    revision = (old["content_revision"] + int(old["fingerprint"] != fingerprint)) if old else max(draft["opportunity"]["content_revision"] for draft, _ in members) + 1
    opportunity.update(content_revision=revision, change_pending=True)
    merged["draft_id"] = f"event_{leader}"
    record = {"active": True, "canonical_post_id": leader, "content_revision": revision, "fingerprint": fingerprint,
              "members": fingerprint_members, "field_sources": provenance, "updates": updates, "draft": merged}
    if old and old["fingerprint"] != fingerprint:
        archive = data_dir / "event_group_history" / leader / f"{old['content_revision']}.json"
        if not archive.exists():
            atomic_json(archive, old)
    atomic_json(path, record)
    return record


def consolidate(data_dir: Path, drafts: list[tuple[dict, dict]]):
    for path in (data_dir / "event_groups").glob("*.json"):
        old = json.loads(path.read_text(encoding="utf-8"))
        old["active"] = False
        atomic_json(path, old)
    groups = []
    for item in sorted(drafts, key=lambda item: (item[1].get("posted_at") or "", item[1]["source_post_id"])):
        group = next((group for group in groups if compatible(group, item)), None)
        if group is None:
            groups.append([item])
        else:
            group.append(item)
    return [merge_group(data_dir, group) for group in groups if len(group) > 1]


def display_draft(data_dir: Path, draft: dict) -> dict:
    path = data_dir / "event_groups" / f"{draft['opportunity']['source_post_id']}.json"
    if path.exists():
        group = json.loads(path.read_text(encoding="utf-8"))
        if group.get("active", True):
            return group["draft"]
    return draft
