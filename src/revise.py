"""③ 후보 고치기 (이슈 #12): 대화(와 ④ 검증 결과)로 고른 후보의 수정안을 만든다.

- 후보 원본(research run의 topics[])은 v1이다. 수정안은 kind "revision" 결과로 따로 저장하고 원본은 바꾸지 않는다.
  revision = {research_run_id, topic_id, base_run_id, version, topic, changes, reason, from_verify}
  topic은 고친 뒤의 전체 후보, changes는 바뀐 필드만 [{field, before, after, before_ko, after_ko}].
- 후보 필드는 검색어·검증에 쓰는 영어로 두고, 화면용 한국어는 topic.ko와 changes[].*_ko에 둔다 (이슈 #39).
- 화면이 [적용]을 누르면 picks.revision_run_id로 이 결과를 가리킨다. 서버에 따로 적용 단계는 없다.
- ④·⑤는 resolve()로 지금 버전의 후보를 얻는다. 검증을 다시 하라고 강제하지 않는다 (사용자 판단).
"""
import json

from pydantic import BaseModel

from src import config, llm

FIELDS = ("title", "summary", "gap", "approach", "evaluation")
KO_FIELDS = ("summary", "gap", "approach", "evaluation")  # title은 논문 제목처럼 영어로 둔다
MAX_HISTORY_MESSAGES = 12  # 수정안에 넣는 최근 대화 (src/chat.py와 같은 값)
MAX_MESSAGE_CHARS = 2_000


class Proposal(BaseModel):
    """바꾸지 않는 필드는 빈 문자열. 바꾸는 필드는 고친 뒤의 전체 문장."""
    title: str
    summary: str
    gap: str
    approach: str
    evaluation: str
    summary_ko: str
    gap_ko: str
    approach_ko: str
    evaluation_ko: str
    reason: str


def version(revision: dict | None) -> int:
    return (revision or {}).get("version") or 1


def resolve(research_run: dict, topic_id: str, revision_run: dict | None = None) -> tuple[dict | None, int]:
    """지금 버전의 후보와 버전 번호. 후보가 없으면 (None, 1). revision_run이 이 후보의 것이 아니면 ValueError."""
    topic = next((t for t in research_run.get("topics") or [] if t.get("id") == topic_id), None)
    if topic is None or revision_run is None:
        return topic, 1
    if (revision_run.get("kind") != "revision" or revision_run.get("topic_id") != topic_id
            or revision_run.get("research_run_id") != research_run.get("run_id")):
        raise ValueError("revision does not belong to this topic")
    revised = revision_run["topic"]
    return {**topic, **{k: revised.get(k, topic.get(k)) for k in FIELDS},
            "ko": revised.get("ko", topic.get("ko") or {})}, version(revision_run)


def _verify_text(verify_run: dict) -> str:
    """수정안에 넘길 검증 결과: 판정, 최근접 논문과 요소별 일치, 비평의 검토 필드."""
    lines = [f"verdict: {verify_run.get('verdict')}"]
    for p in verify_run.get("nearest") or []:
        lines.append(f"[{p.get('id')}] {p.get('title')} ({p.get('year')})")
        for k, e in (p.get("comparison") or {}).items():
            if e:
                lines.append(f"  {k}: {e.get('match')} - {e.get('reason', '')}")
    c = verify_run.get("critique") or {}
    for k in ("unsupported", "vague", "already_done", "suggestion"):
        if c.get(k):
            lines.append(f"critique.{k}: {c[k]}")
    return "\n".join(lines)


def propose(profile: dict, topic: dict, history: list[dict], verify_run: dict | None = None) -> dict:
    """{changes: [{field, before, after}], topic, reason}. 바뀐 게 없으면 changes가 빈 목록."""
    recent = [{"role": m["role"], "content": m["content"][:MAX_MESSAGE_CHARS]} for m in history[-MAX_HISTORY_MESSAGES:]]
    parts = [f"Lab profile:\n{json.dumps(profile, ensure_ascii=False)}",
             f"Current candidate:\n{json.dumps({k: topic.get(k) or '' for k in FIELDS}, ensure_ascii=False, indent=1)}"]
    if verify_run:
        parts.append(f"Verification result of this candidate:\n{_verify_text(verify_run)}")
    parts.append(f"Conversation (latest last):\n{json.dumps(recent, ensure_ascii=False)}")
    system = (config.PROMPT_DIR / "topic_revise.md").read_text("utf-8")
    got = llm.chat_json([{"role": "system", "content": system}, {"role": "user", "content": "\n\n".join(parts)}], Proposal)
    old_ko = topic.get("ko") or {}
    changes = []
    for k in FIELDS:
        after, before = getattr(got, k).strip(), (topic.get(k) or "").strip()
        if after and after != before:
            change = {"field": k, "before": before, "after": after}
            if k in KO_FIELDS:
                change.update(before_ko=old_ko.get(k) or "", after_ko=getattr(got, f"{k}_ko").strip())
            changes.append(change)
    revised = {**{k: topic.get(k) or "" for k in FIELDS}, **{c["field"]: c["after"] for c in changes}}
    # 바꾸지 않은 필드는 원래 한국어를 쓴다. 바꾼 필드의 한국어가 비면 그 필드는 영어로 보인다 (화면이 영어로 대신한다)
    revised["ko"] = {k: old_ko.get(k) or "" for k in KO_FIELDS}
    revised["ko"].update({c["field"]: c["after_ko"] for c in changes if "after_ko" in c})
    return {"changes": changes, "topic": revised, "reason": got.reason.strip()}
