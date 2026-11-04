"""Caption-first intent rules: stale poster calls to action cannot reopen a closed call."""

import re

CURRENT_ACTION = re.compile(
    r"\b(?:apply\s+(?:now|today|by|through|via|to join)|register\s+(?:now|today|here|through|via|by|for)|"
    r"(?:applications?|registration)\s+(?:(?:are|is)\s+)?(?:now\s+)?open|open for registration|"
    r"registration (?:link|closes)|sign[ -]?up\s+(?:now|here|through)|rsvp|we are hiring|we're hiring)\b", re.I)
RETROSPECTIVE = re.compile(
    r"\b(?:thank(?:s| you).{0,55}(?:applied|attended|joined)|(?:event |workshop )?recap|"
    r"was (?:a |an )?(?:success|amazing)|we had the hono[u]?r of hosting|"
    r"highlights from|throwback to|congratulations to|that.?s a wrap|applications? (?:have |are )?closed)\b", re.I)
TEASER = re.compile(r"\b(?:more details coming soon|details (?:coming|to follow)|stay tuned(?: for more)?|save the date|coming soon)\b", re.I)
PUBLIC_INVITATION = re.compile(r"\b(?:open to (?:everyone|all students|all undergraduates|all(?=\s*(?:[.!?]|$)))|everyone (?:is )?welcome|all students (?:are )?welcome|drop[ -]?ins? (?:are )?welcome|public meeting|open house)\b", re.I)
INTERNAL_MEETING = re.compile(r"\b(?:(?:annual|yearly)\s+)?general\s+meeting\b|\b(?:internal|executive|exec|committee|members?[ -]only|members?|developers?|club|team)\s+meeting\b", re.I)


def exclusion_reason(caption: str | None) -> str | None:
    text = " ".join((caption or "").split())
    if not text:
        return None
    private_only = re.search(r"\bmembers?[ -]only\b|\bfor (?:existing|current) members\b", text, re.I)
    if INTERNAL_MEETING.search(text) and (private_only or not PUBLIC_INVITATION.search(text)):
        return "internal_club_meeting"
    action = CURRENT_ACTION.search(text)
    if RETROSPECTIVE.search(text) and not action:
        return "retrospective_or_closed_call"
    if TEASER.search(text) and not action:
        return "announcement_only"
    return None
