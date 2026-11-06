"""대화 기록을 Solar 입력으로 바꾸는 규칙과 자동 압축 (이슈 #31).

메시지는 두 종류다.
- 상태 카드: kind가 있는 메시지 (problems, problem, topics, verify, final, profile). 단계를 바꾸는 결과이고 요약하지 않는다.
  채팅에는 한 줄 표시로 순서대로 남긴다. 자세한 내용은 저장된 결과에서 system 컨텍스트로 들어간다.
  profile 카드는 프로필이 따로 들어가므로 뺀다. topics 카드의 본문(③ 발표문)은 후보 제목 한 줄로 바꾼다.
- 주변 대화: kind가 없는 질문과 답변. 원문으로 넣고, 길어지면 압축한다.

압축 (memo): 화면이 세션에 저장해 요청마다 보낸다. 서버는 상태를 갖지 않는다.
  memo = {"items": {field: [{id, text, source}]}, "through": 마지막으로 요약한 메시지 id}
  - 항목 목록이다 (FIELDS). 반복 압축 때 LLM은 기존 목록과 새 대화를 보고 변화(add, update, remove)만 돌려주고,
    코드가 목록에 적용한다. 근거(source)가 이번에 요약한 메시지가 아니면 버린다. 그래서 사용자가 바꾸지 않은 항목은
    몇 번을 압축해도 남는다.
  - through가 history에 없으면 (요약 범위 안의 메시지를 재시도한 경우 등) memo를 쓰지 않는다.
"""
import json

from pydantic import BaseModel

from src import config, llm, runs

CARD_KINDS = ("profile", "problems", "problem", "topics", "verify", "final")
CARD_CHARS = 300          # 카드 한 줄 표시의 최대 글자 수
COMPACT_TOKENS = 50_000   # 직전 채팅 호출의 입력 토큰이 이보다 크면 화면이 압축을 요청한다
KEEP_RECENT = 6           # 압축할 때 원문으로 남기는 최근 메시지 수
HARD_LIMIT_CHARS = 120_000  # memo를 적용한 뒤에도 대화가 이보다 길면 오래된 주변 대화를 묶어서 버린다 (압축 실패 대비)
DROP_BLOCK = 6

FIELDS = {"conditions": "조건", "preferences": "선호", "rejected": "거절한 방향", "open_questions": "미해결 질문"}
FIELD_LIMIT = 12          # 칸마다 상한. 넘으면 오래된 항목부터 뺀다 (rejected는 빼지 않는다)

# ③ 후보 생성에 넣는 대화
RESEARCH_USER_CHARS = 2_000   # 최근 사용자 메시지 합계 상한
RESEARCH_REPLY_CHARS = 300    # ③ 요청 직전 채팅 답변


def is_card(m: dict) -> bool:
    return bool(m.get("kind"))


def _topic_titles(run_id: str | None) -> list[str]:
    run = runs.load(run_id) if run_id else None
    return [t.get("title") or "" for t in (run or {}).get("topics") or []]


def card_line(m: dict) -> str | None:
    """상태 카드 → 한 줄 표시. None이면 넣지 않는다."""
    kind = m.get("kind")
    if kind == "profile":
        return None
    if kind == "topics":
        titles = _topic_titles(m.get("run_id"))
        return f"[③ 연구 주제 후보 {len(titles)}개를 보여 줌: {' / '.join(titles)}]" if titles else "[③ 연구 주제 후보를 보여 줌]"
    text = " ".join((m.get("content") or "").split())
    return f"[{text[:CARD_CHARS]}]"


def _plain(m: dict) -> dict | None:
    """LLM에 넘길 {role, content}. 카드는 한 줄 표시로."""
    if is_card(m):
        line = card_line(m)
        return {"role": "assistant", "content": line} if line else None
    return {"role": m["role"], "content": m["content"]}


def _through_index(history: list[dict], memo: dict | None) -> int | None:
    through = (memo or {}).get("through")
    return next((i for i, m in enumerate(history) if m.get("id") and m.get("id") == through), None)


def memo_text(memo: dict | None) -> str:
    items = (memo or {}).get("items") or {}
    lines = [f"- {label}: {it['text']}" for f, label in FIELDS.items() for it in items.get(f) or []]
    return "\n".join(lines)


def chat_messages(history: list[dict], memo: dict | None = None) -> tuple[list[dict], str]:
    """채팅용 대화 → (messages, 요약문). 요약 범위 안에서는 카드 한 줄만 남기고 주변 대화는 요약으로 대신한다.
    history: common.history(meta=True) 결과 (id, kind, run_id 포함)."""
    cut = _through_index(history, memo)
    summary = memo_text(memo) if cut is not None else ""
    kept = []  # (카드인지, 메시지)
    for i, m in enumerate(history):
        if cut is not None and i <= cut and not is_card(m):
            continue
        p = _plain(m)
        if p:
            kept.append((is_card(m), p))
    # 압축이 실패했거나 아직이라 너무 길면 오래된 주변 대화를 묶어서 버린다. 카드는 남긴다.
    while sum(len(p["content"]) for _, p in kept) > HARD_LIMIT_CHARS:
        drop = [i for i, (card, _) in enumerate(kept) if not card][:DROP_BLOCK]
        if not drop:
            break
        kept = [x for i, x in enumerate(kept) if i not in drop]
    return [p for _, p in kept], summary


def research_conversation(history: list[dict], memo: dict | None = None) -> dict:
    """③ 후보 생성에 넣는 대화 (약 130회 호출에 반복해서 들어간다).
    요약, 최근 사용자 메시지(합계 RESEARCH_USER_CHARS), ③ 요청 직전 채팅 답변 앞부분, 이전에 보여 준 후보 제목.
    카드(프로필, 추천 목록, ③ 발표문 등)는 넣지 않는다."""
    cut = _through_index(history, memo)
    chat = [m for i, m in enumerate(history) if not is_card(m) and (cut is None or i > cut)]
    users, used = [], 0
    for m in reversed([m for m in chat if m["role"] == "user"]):
        text = m["content"].strip()
        if used + len(text) > RESEARCH_USER_CHARS:
            if not users:
                users.append(text[:RESEARCH_USER_CHARS])
            break
        users.append(text)
        used += len(text)
    reply = next((m["content"] for m in reversed(chat) if m["role"] == "assistant"), "")
    previous = [t for m in history if m.get("kind") == "topics" for t in _topic_titles(m.get("run_id"))]
    out = {"recent_user_messages": users[::-1]}
    if reply:
        out["last_assistant_reply"] = reply[:RESEARCH_REPLY_CHARS]
    summary = memo_text(memo) if cut is not None else ""
    if summary:
        out["earlier_conversation_summary"] = summary
    if previous:
        out["previous_candidates"] = previous
    return out


# ---------- 압축 ----------
class Change(BaseModel):
    op: str          # add | update | remove
    field: str       # FIELDS 중 하나
    item_id: str = ""  # update, remove 대상
    text: str = ""
    source: str = ""   # 근거 메시지 id (이번에 요약하는 메시지 중 하나)


class Delta(BaseModel):
    changes: list[Change]


def compact_range(history: list[dict], memo: dict | None) -> list[dict]:
    """이번에 요약할 주변 대화: 지난 요약 이후, 최근 KEEP_RECENT개 앞까지. id가 있는 것만."""
    cut = _through_index(history, memo)
    end = max(0, len(history) - KEEP_RECENT)
    start = cut + 1 if cut is not None else 0
    return [m for m in history[start:end] if m.get("id")]


def apply_delta(memo: dict | None, delta: Delta, sources: set[str], through: str) -> dict:
    """LLM이 준 변화를 목록에 적용한다. 근거가 이번 범위에 없거나 대상이 없는 변화는 버린다."""
    old = (memo or {}).get("items") or {}
    items = {f: [dict(it) for it in old.get(f) or []] for f in FIELDS}
    n = 1 + max([int(it["id"][1:]) for f in items for it in items[f] if str(it.get("id", "")).startswith("i")
                 and it["id"][1:].isdigit()] or [0])
    applied = []
    for c in delta.changes:
        field, op, text = c.field.strip(), c.op.strip().lower(), " ".join(c.text.split())
        if field not in FIELDS or c.source.strip() not in sources:
            continue
        if op == "add" and text:
            items[field].append({"id": f"i{n}", "text": text, "source": c.source.strip()})
            n += 1
        elif op in ("update", "remove"):
            target = next((it for it in items[field] if it["id"] == c.item_id.strip()), None)
            if target is None:
                continue
            if op == "update" and text:
                target.update(text=text, source=c.source.strip())
            elif op == "remove":
                items[field].remove(target)
            else:
                continue
        else:
            continue
        applied.append(c.model_dump())
    for f in FIELDS:
        if f != "rejected" and len(items[f]) > FIELD_LIMIT:
            items[f] = items[f][-FIELD_LIMIT:]
    return {"items": items, "through": through, "applied": applied}


def compact(history: list[dict], memo: dict | None = None) -> dict | None:
    """요약할 주변 대화가 있으면 Solar 1회로 변화를 받아 새 memo를 돌려준다. 없으면 None."""
    segment = compact_range(history, memo)
    chat = [m for m in segment if not is_card(m)]
    if not chat:
        return None
    lines = []
    for m in segment:  # 카드도 순서를 알 수 있게 한 줄로 보여 준다 (근거로는 주변 대화만 쓴다)
        p = _plain(m)
        if p:
            lines.append(f"[{m['id']}] {p['role']}: {p['content']}")
    current = {f: [{"id": it["id"], "text": it["text"]} for it in ((memo or {}).get("items") or {}).get(f) or []]
               for f in FIELDS}
    user = (f"Current items:\n{json.dumps(current, ensure_ascii=False)}\n\n"
            f"New conversation (message id in brackets):\n" + "\n".join(lines))
    system = (config.PROMPT_DIR / "chat_compact.md").read_text(encoding="utf-8")
    delta = llm.chat_json([{"role": "system", "content": system}, {"role": "user", "content": user}], Delta)
    return apply_delta(memo, delta, {m["id"] for m in chat}, segment[-1]["id"])
