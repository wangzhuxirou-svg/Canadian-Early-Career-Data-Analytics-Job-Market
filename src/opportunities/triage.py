"""Explainable offline routing, with automatic candidates distinct from human approval."""

import argparse
import html
import json
import re
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from src.opportunities.drafts import atomic_json, has_usable_evidence, source_is_current, store_draft, track_source
from src.opportunities.extract import DATA_DIR, _source_parts, build_draft

POLICY_VERSION = "opportunity_triage_v2"
ZONE = ZoneInfo("America/Toronto")
RELATIVE_DATE = re.compile(r"\b(?:next (?:monday|tuesday|wednesday|thursday|friday|saturday|sunday|week|weekend)|today|tonight|tomorrow|this (?:week|weekend)|\d+\s+(?:days?|hours?) left)\b", re.I)
VIDEO_WARNING = "Video content is metadata-only; video downloading is not implemented."
REASON_LABELS = {
    "internal_club_meeting": "社团内部成员会议，不属于公开学习或参加机会",
    "current_human_approval": "当前版本已经人工批准",
    "human_rejected": "当前版本已被人工排除",
    "deadline_updated_by_later_posts": "后续帖子更新了截止日期；旧日期和各帖来源均保留",
    "expired_relative_window": "按发帖时间判断，相对日期所指的活动时段已过",
    "ocr_failed_this_run": "本轮 OCR 失败，使用已取得的正文，图片信息未确认",
    "retrospective_or_closed_call": "回顾、感谢申请者或已经结束的招募",
    "announcement_only": "只有预告，尚未开放参加或申请",
    "same_event_merged": "同一活动的补充帖子，已合并",
    "year_anchored_to_publication": "未写年份，按发帖日期就近确定；原日期文字保留",
    "optional_fields_conflict_use_source": "非关键字段有差异，请查看原帖",
    "skills_need_caption_confirmation": "仅由 OCR 识别的技能不用于自动候选标签",
    "explicit_caption_and_validation_passed": "正文依据明确，规则与格式校验通过",
    "no_actionable_opportunity": "没有明确可参加或申请的机会",
    "expired_deadline": "报名或申请已截止", "expired_start": "活动开始日期已过", "expired_end": "活动已结束",
    "conflicting_evidence": "来源字段存在冲突", "missing_title": "缺少明确标题",
    "missing_start_date": "缺少明确活动日期", "missing_deadline_date": "缺少明确申请截止日期",
    "missing_location_or_online_format": "缺少地点或线上参加方式", "unclear_participation_route": "参加或报名方式不明确",
    "relative_date_needs_confirmation": "相对日期需要核实（如 next Friday）",
    "inferred_year_needs_confirmation": "日期年份由发帖时间推定，需要核实",
    "unresolved_collection_gap": "部分内容采集失败", "unresolved_image_text": "图片文字尚未可靠识别",
    "source_snapshot_needs_refresh": "本地快照已超过自动候选的新鲜度要求",
    "no_accessible_evidence": "没有可读取的证据，需要先补采集",
    "existing_human_decision": "已有人工决定，禁止自动覆盖",
    "mixed_retrospective_and_action": "回顾内容与行动提示混杂，需要核实",
    "source_version_not_current": "草稿与当前源版本不一致",
    "title_needs_caption_confirmation": "标题缺少正文依据",
    "start_date_needs_caption_confirmation": "活动日期缺少正文依据",
    "deadline_date_needs_caption_confirmation": "截止日期缺少正文依据",
}


def assess(draft: dict, raw: dict, data_dir: Path = DATA_DIR,
           now: datetime | None = None) -> dict:
    """Re-evaluated at use time: saved decisions cannot keep expired data eligible."""
    from src.opportunities.review import ORGANIZERS_PATH, _validator
    from src.opportunities.scan import RETROSPECTIVE_TERMS, opportunity_relevance
    from config.settings import SOFT_SKILL_KEYWORDS, TECHNICAL_SKILLS_KEYWORDS

    now = now or datetime.now(timezone.utc)
    today = now.astimezone(ZONE).date()
    opportunity = draft["opportunity"]
    sources = [raw]
    for post_id in opportunity.get("source_post_ids", []):
        path = data_dir / f"{post_id}.json"
        if post_id != raw["source_post_id"] and path.exists():
            sources.append(json.loads(path.read_text(encoding="utf-8")))
    text = "\n".join(part["text"] for source in sources for part in _source_parts(source, data_dir / "ocr"))
    captions = [source.get("caption_raw") or "" for source in sources]
    decision = {"decision": "needs_review", "reasons": [], "warnings": [], "policy_version": POLICY_VERSION,
                "evaluated_at": now.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
                "source_post_id": raw["source_post_id"], "content_hash": raw["content_hash"],
                "content_revision": opportunity["content_revision"]}
    reasons = decision["reasons"]
    warnings = decision["warnings"]
    if list(_validator("opportunity").iter_errors(opportunity)):
        reasons.append("opportunity_contract_invalid")
        return decision
    if list(_validator("raw-post").iter_errors(raw)):
        reasons.append("raw_post_contract_invalid")
    if draft.get("exclusion_reason"):
        decision.update(decision="excluded", reasons=[draft["exclusion_reason"]])
        return decision
    if not text.strip():
        reasons.append("no_accessible_text")
        return decision
    from src.opportunities.content import exclusion_reason
    if exclusion_reason(text) == "internal_club_meeting":
        decision.update(decision="excluded", reasons=["internal_club_meeting"])
        return decision
    if not opportunity.get("start_date") and re.search(r"\bnext week(?:end)?\b", raw.get("caption_raw") or "", re.I) and raw.get("posted_at"):
        posted = datetime.fromisoformat(raw["posted_at"].replace("Z", "+00:00")).astimezone(ZONE).date()
        if (today - posted).days > 14:
            decision.update(decision="excluded", reasons=["expired_relative_window"])
            return decision
    # A poster with readable text is not an unrelated post just because its
    # caption is missing. Non-actionable text remains excluded, not incomplete.
    meeting_heading = re.search(r"\b(?:(?:annual|yearly)\s+)?general\s+meeting\b", " ".join(text.split()), re.I)
    if opportunity_relevance(text, False) == "exclude" and not meeting_heading:
        decision.update(decision="excluded", reasons=["no_actionable_opportunity"])
        return decision
    from src.opportunities.temporal import expiration_reason
    expired = expiration_reason(opportunity, now)
    if expired:
        decision.update(decision="excluded", reasons=[expired])
        return decision
    if not source_is_current(data_dir, opportunity):
        reasons.append("source_version_not_current")
    version_path = data_dir / "draft_versions" / f"{raw['source_post_id']}.json"
    if version_path.exists():
        for issue in json.loads(version_path.read_text(encoding="utf-8")).get("processing_issues", []):
            (warnings if issue == "ocr_failed_this_run" else reasons).append(issue)
    if list(_validator("opportunity-draft").iter_errors(draft)):
        reasons.append("draft_contract_invalid")
    organizers = {item["organizer_id"]: item for item in json.loads(ORGANIZERS_PATH.read_text(encoding="utf-8"))}
    organizer = organizers.get(opportunity["organizer_id"])
    if not organizer or not organizer.get("enabled") or raw.get("organizer_id") != opportunity["organizer_id"]:
        reasons.append("unknown_or_disabled_organizer")
    source = urlparse(opportunity["source_url"])
    if source.scheme != "https" or source.hostname not in {"www.instagram.com", "instagram.com"} or source.path.rstrip("/").split("/")[-1] != raw["source_post_id"]:
        reasons.append("source_url_invalid")
    if set(opportunity["skill_ids"]) - (set(TECHNICAL_SKILLS_KEYWORDS) | set(SOFT_SKILL_KEYWORDS)):
        reasons.append("unknown_skill_reference")
    critical_conflicts = [conflict for conflict in draft["conflicts"] if not any(
        term in conflict for term in ("locations", "formats", "URLs", "eligibility"))]
    if critical_conflicts:
        reasons.append("conflicting_evidence")
    elif draft["conflicts"]:
        warnings.append("optional_fields_conflict_use_source")
    if any(term in " ".join(text.casefold().split()) for term in RETROSPECTIVE_TERMS):
        reasons.append("mixed_retrospective_and_action")
    if opportunity["title"].startswith("Untitled") or not opportunity["title"].strip():
        reasons.append("missing_title")
    required_date = "start_date" if opportunity["kind"] == "event" else "deadline_date"
    if not opportunity.get(required_date):
        (reasons if opportunity["kind"] == "ongoing_opportunity" else warnings).append(f"missing_{required_date}")
    if opportunity["kind"] == "event" and not opportunity.get("location") and opportunity.get("format") != "online":
        warnings.append("missing_location_or_online_format")
    # The official source post is an acceptable entry point for link-in-bio;
    # never fabricate a direct registration URL.
    if not opportunity.get("registration_url") and not re.search(r"link in (?:our |the )?bio|drop[ -]?ins? (?:are )?(?:welcome|allowed)|no registration (?:is )?required|walk[ -]?ins?", text, re.I):
        warnings.append("unclear_participation_route")
    url = opportunity.get("registration_url")
    if url and (urlparse(url).scheme not in {"https", "http"} or not urlparse(url).hostname):
        reasons.append("unsafe_registration_url")
    if RELATIVE_DATE.search(text) and not opportunity.get("deadline_date"):
        reasons.append("relative_date_needs_confirmation")
    if any("year_from_post" in item["rule_id"] for item in draft["evidence"]):
        warnings.append("year_anchored_to_publication")
    # Only complete caption evidence is automatically accepted. OCR-only
    # candidates remain reviewable; EasyOCR's success threshold is not a fact check.
    for field in ("title", required_date):
        if field != "title" and not opportunity.get(field):
            continue
        if not any(item["field"] == field and item["source_part"] == "caption"
                   and item["candidate_value"] == opportunity.get(field)
                   and any(item["evidence_text"] in caption for caption in captions) for item in draft["evidence"]):
            (reasons if field == "title" else warnings).append(f"{field}_needs_caption_confirmation")
    if not any(source.get("caption_status") == "available" for source in sources):
        reasons.append("caption_unavailable")
    caption_skills = {skill for item in draft["evidence"] if item["field"] == "skill_ids"
                      and item["source_part"] == "caption" for skill in item["candidate_value"]}
    if set(opportunity["skill_ids"]) - caption_skills:
        warnings.append("skills_need_caption_confirmation")
    errors = raw.get("errors", [])
    video_only_gap = bool(raw.get("media")) and all(item.get("type") == "video" for item in raw["media"])
    if raw.get("collection_status") != "complete" and not (video_only_gap and errors and all(error == VIDEO_WARNING for error in errors)):
        warnings.append("unresolved_collection_gap")
    for media in raw.get("media", []):
        if media.get("type") != "image":
            continue
        results = [json.loads(path.read_text(encoding="utf-8")) for path in (data_dir / "ocr").glob("*.json")]
        matching = [result for result in results if result.get("image_hash") == media.get("sha256")]
        if media.get("download_status") != "downloaded" or not matching or max(matching, key=lambda result: result.get("processed_at", "")).get("status") != "success":
            warnings.append("unresolved_image_text")
            break
    try:
        fetched = max(datetime.fromisoformat(source["last_fetched_at"].replace("Z", "+00:00")) for source in sources)
        if now - fetched > timedelta(days=3) or fetched - now > timedelta(minutes=5):
            reasons.append("source_snapshot_needs_refresh")
    except (KeyError, ValueError):
        reasons.append("unknown_snapshot_time")
    ledger_paths = [data_dir / "review_overrides" / f"{opportunity['opportunity_id']}.json"]
    for source in sources:
        path = data_dir / "drafts" / f"{source['source_post_id']}.json"
        if path.exists():
            source_draft = json.loads(path.read_text(encoding="utf-8"))
            ledger_paths.append(data_dir / "review_overrides" / f"{source_draft['opportunity']['opportunity_id']}.json")
    if any(path.exists() for path in ledger_paths):
        # A human hold/rejection or an old human decision must never be bypassed
        # by automatic reclassification. Human approval is handled by review.py.
        reasons.append("existing_human_decision")
    if not reasons:
        decision["decision"] = "auto_candidate"
        reasons.append("explicit_caption_and_validation_passed")
    decision["reasons"] = list(dict.fromkeys(reasons))
    decision["warnings"] = list(dict.fromkeys(warnings))
    return decision


def save_decision(data_dir: Path, draft: dict, raw: dict, now=None) -> dict:
    decision = assess(draft, raw, data_dir, now)
    atomic_json(data_dir / "triage" / f"{raw['source_post_id']}.json", decision)
    return decision


def process_local_post(data_dir: Path, raw: dict, now=None, revision=None, version=None) -> dict:
    from src.opportunities.extract import _excluded_draft
    version = version or track_source(data_dir, raw, revision)
    draft = build_draft(raw, data_dir / "ocr")
    if draft is None:
        draft = _excluded_draft(raw, "no_actionable_opportunity")
        if not (raw.get("caption_raw") or "").strip():
            draft["exclusion_reason"] = None
            draft["opportunity"]["title"] = "Unclassified post (needs evidence)"
    store_draft(data_dir, raw, draft, version)
    if not has_usable_evidence(raw, data_dir):
        decision = {"source_post_id": raw["source_post_id"], "content_hash": raw["content_hash"],
                    "content_revision": version["content_revision"], "decision": "needs_review",
                    "reasons": ["no_accessible_evidence"], "policy_version": POLICY_VERSION}
        atomic_json(data_dir / "triage" / f"{raw['source_post_id']}.json", decision)
        return decision
    saved = json.loads((data_dir / "drafts" / f"{raw['source_post_id']}.json").read_text(encoding="utf-8"))
    return save_decision(data_dir, saved, raw, now)


def reprocess(data_dir: Path = DATA_DIR, now=None) -> dict:
    """Rebuild from local raw/OCR only; retain original snapshots and decisions."""
    now = now or datetime.now(timezone.utc)
    records = []
    candidates = []
    raw_posts = {}
    for path in sorted(data_dir.glob("*.json")):
        raw = json.loads(path.read_text(encoding="utf-8"))
        if raw.get("source") != "instagram" or not raw.get("source_post_id"):
            continue
        decision = process_local_post(data_dir, raw, now)
        raw_posts[raw["source_post_id"]] = raw
        records.append(decision)
    from src.opportunities.events import consolidate, display_draft
    by_id = {item["source_post_id"]: item for item in records}
    available = []
    for post_id, raw in raw_posts.items():
        path = data_dir / "drafts" / f"{post_id}.json"
        # Expired posts still provide identity/date evidence for undated calls.
        # Consolidate first, then assess the combined event for expiry.
        temporal_exclusions = {"expired_deadline", "expired_start", "expired_end"}
        if path.exists() and (by_id[post_id]["decision"] != "excluded" or
                              set(by_id[post_id]["reasons"]) <= temporal_exclusions):
            draft = json.loads(path.read_text(encoding="utf-8"))
            if not draft.get("exclusion_reason"):
                available.append((draft, raw))
    for group in consolidate(data_dir, available):
        leader = group["canonical_post_id"]
        decision = assess(group["draft"], raw_posts[leader], data_dir, now)
        if group.get("updates"):
            decision["warnings"].append("deadline_updated_by_later_posts")
        by_id[leader].update(decision)
        by_id[leader]["source_post_ids"] = group["draft"]["opportunity"]["source_post_ids"]
        atomic_json(data_dir / "triage" / f"{leader}.json", by_id[leader])
        for member in group["members"]:
            if member["source_post_id"] == leader:
                continue
            record = by_id[member["source_post_id"]]
            record.update(decision="merged_duplicate", canonical_post_id=leader, reasons=["same_event_merged"])
            atomic_json(data_dir / "triage" / f"{member['source_post_id']}.json", record)
    for record in records:
        if record["decision"] not in {"excluded", "merged_duplicate"}:
            post_id = record["source_post_id"]
            draft_path = data_dir / "drafts" / f"{post_id}.json"
            if not draft_path.exists():
                continue
            draft = display_draft(data_dir, json.loads(draft_path.read_text(encoding="utf-8")))
            from src.opportunities.review import effective_approved_opportunity
            approved = effective_approved_opportunity(draft, data_dir)
            if approved:
                checked = assess({**draft, "opportunity": approved}, raw_posts[post_id], data_dir, now)
                if checked["decision"] == "excluded" or set(checked["reasons"]) & {
                    "unknown_or_disabled_organizer", "source_version_not_current", "source_url_invalid",
                    "raw_post_contract_invalid", "opportunity_contract_invalid"}:
                    record.update(checked)
                else:
                    record.update(decision="human_approved", reasons=["current_human_approval"])
                    candidates.append({"opportunity": approved, "eligibility": record})
            elif record["decision"] == "auto_candidate":
                candidates.append({"opportunity": candidate_projection(draft), "eligibility": record})
            else:
                ledger_path = data_dir / "review_overrides" / f"{draft['opportunity']['opportunity_id']}.json"
                if ledger_path.exists():
                    ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
                    if ledger.get("content_revision") == draft["opportunity"]["content_revision"] and ledger.get("review_status") == "rejected":
                        record.update(decision="excluded", reasons=["human_rejected"])
            atomic_json(data_dir / "triage" / f"{post_id}.json", record)
    report = {"generated_at": now.isoformat(), "policy_version": POLICY_VERSION,
              "counts": dict(Counter(item["decision"] for item in records)), "records": records}
    atomic_json(data_dir / "triage-report.json", report)
    atomic_json(data_dir / "publishable-candidates.json", {"generated_at": now.isoformat(), "policy_version": POLICY_VERSION, "candidates": candidates})
    write_report_html(data_dir, report)
    return report


def candidate_projection(draft: dict) -> dict:
    """Optional OCR-only tags must not turn into asserted recommendation skills."""
    opportunity = {**draft["opportunity"], "change_pending": False}
    for field in ("skill_ids", "activity_type_ids", "topic_ids"):
        supported = set()
        for item in draft["evidence"]:
            if item["field"] == field and item["source_part"] == "caption":
                value = item["candidate_value"]
                supported.update(value if isinstance(value, list) else [value])
        opportunity[field] = [value for value in opportunity[field] if value in supported]
    return opportunity


def write_report_html(data_dir: Path, report: dict) -> Path:
    from src.opportunities.events import display_draft
    sections = []
    for status, label in (("auto_candidate", "自动可发布候选"), ("human_approved", "已人工批准"), ("needs_review", "需要人工核对或补充证据"), ("excluded", "不展示")):
        cards = []
        for record in report["records"]:
            if record["decision"] != status:
                continue
            post_id = record["source_post_id"]
            raw = json.loads((data_dir / f"{post_id}.json").read_text(encoding="utf-8"))
            draft_path = data_dir / "drafts" / f"{post_id}.json"
            draft = json.loads(draft_path.read_text(encoding="utf-8")) if draft_path.exists() else {}
            draft = display_draft(data_dir, draft) if draft else {}
            opportunity = draft.get("opportunity", {})
            reasons = "；".join(REASON_LABELS.get(reason, reason) for reason in record["reasons"])
            warnings = "；".join(REASON_LABELS.get(reason, reason) for reason in record.get("warnings", []))
            # Always derive this link from the ID; raw source URLs are not HTML.
            from urllib.parse import quote
            link = "https://www.instagram.com/p/" + quote(post_id, safe="") + "/"
            source_ids = opportunity.get("source_post_ids", [post_id])
            sources = " · ".join(f'<a href="https://www.instagram.com/p/{quote(source_id, safe="")}/" rel="noreferrer">原帖 {index + 1}</a>'
                                  for index, source_id in enumerate(source_ids))
            local_clock = lambda value: datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(ZONE).strftime("%H:%M") if value else "未知"
            time_text = f'{local_clock(opportunity.get("start_at"))}–{local_clock(opportunity.get("end_at"))}'
            captions = []
            for source_id in source_ids:
                source_path = data_dir / f"{source_id}.json"
                if source_path.exists():
                    source_raw = json.loads(source_path.read_text(encoding="utf-8"))
                    captions.append(f'<h4>{html.escape(source_id)}</h4><pre>{html.escape(source_raw.get("caption_raw") or "正文未取得；可查看下载图片和 OCR。")}</pre>')
            cards.append(f'<article><h3>{html.escape(opportunity.get("title") or post_id)}</h3>'
                         f'<p><a href="{link}" rel="noreferrer">打开官方原帖</a> · {html.escape(raw["organizer_id"])} · {html.escape(post_id)}</p>'
                         f'<p>来源（同一活动只展示一张卡）：{sources}</p>'
                         f'<p><strong>原因：</strong>{html.escape(reasons)}</p>'
                         f'<p><strong>信息提示：</strong>{html.escape(warnings or "无")}</p>'
                         f'<p>活动日期：{html.escape(str(opportunity.get("start_date") or "未知"))} · 截止日期：{html.escape(str(opportunity.get("deadline_date") or "未知"))} · 地点：{html.escape(opportunity.get("location") or "未知")}</p>'
                         f'<p>活动时间：{html.escape(time_text)}（America/Toronto）；截止时刻：{html.escape(local_clock(opportunity.get("deadline_at")))}</p>'
                         f'<details><summary>查看本地采集的正文</summary>{"".join(captions)}</details></article>')
        sections.append(f'<section><h2>{label}（{len(cards)}）</h2>{"".join(cards)}</section>')
    page = ('<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>D6 活动分流结果</title>'
            '<style>body{font:16px system-ui;max-width:1100px;margin:32px auto;padding:0 20px;color:#172133}article{border:1px solid #ccd4df;border-radius:10px;padding:18px;margin:16px 0}pre{white-space:pre-wrap}h2{margin-top:40px}a{color:#175da8}</style>'
            '<h1>D6 活动分流结果</h1><p>基于现有本地正文和 OCR，未联网重查。自动候选不等于人工批准或已经上线。缺少的可选信息留为未知，可点击原帖继续了解。时间和源内容变化后须重新校验。</p>'
            '<p><a href="review/triage-current/review.html">打开本轮人工审核材料</a> · <a href="publishable-candidates.json">自动候选 JSON</a></p>'
            f'<p>生成时间：{html.escape(report["generated_at"])}</p><p>另有 {report["counts"].get("merged_duplicate", 0)} 篇重复或补充帖子已合并到相应活动，不单独重复展示。</p>{"".join(sections)}</html>')
    path = data_dir / "triage-report.html"
    path.write_text(page, encoding="utf-8")
    return path


def main() -> None:
    parser = argparse.ArgumentParser(description="Rebuild and triage existing local opportunities without network access.")
    parser.parse_args()
    report = reprocess()
    print(json.dumps({"counts": report["counts"], "report": str(DATA_DIR / "triage-report.json"),
                      "candidates": str(DATA_DIR / "publishable-candidates.json")}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
