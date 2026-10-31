"""Open Problem 추천 챗봇.

1턴: 연구실 프로필 입력 → Open Problem 카드. 2턴: Open Problem 선택 → 연구 주제 후보.
이후: 추천과 주제 후보를 근거로 후속 대화 (도구 연결 없음).
실행: streamlit run app.py
"""
import streamlit as st

from src.chat import reply_stream
from src.recommend import recommend
from src.topic import generate_topics

st.set_page_config(page_title="Open Problem 추천", page_icon="🔎")


@st.cache_data(show_spinner=False)
def cached_recommend(lab_name: str, interests: str, extra: str) -> dict:
    return recommend(lab_name, interests, extra)


@st.cache_data(show_spinner=False)
def cached_topics(lab_name: str, interests: str, extra: str, problem_id: str) -> dict:
    return generate_topics(lab_name, interests, extra, problem_id)


def reset() -> None:
    for key in ("profile", "result", "messages", "topic_result"):
        st.session_state.pop(key, None)


def choose_other_problem() -> None:
    st.session_state.pop("topic_result", None)
    st.session_state["messages"] = []


def render_card(i: int, r: dict, selectable: bool) -> str | None:
    """selectable이면 선택 버튼을 보여 주고, 눌렸을 때 problem_id를 돌려준다."""
    with st.container(border=True):
        st.markdown(f"#### {i}. {r['title']}")
        paper = r["paper"]
        st.caption(f"{paper['title']} ({paper['year']}) · {r['section_title']} · p.{r['page']} · "
                   f"[arXiv]({paper['url']})")
        if r["connection"]:
            st.markdown(f"**연결** · {r['connection']}")
        if r["reason"]:
            st.markdown(r["reason"])
        st.markdown(f"**Open Problem** · {r['summary']}")
        st.markdown(" ".join(f"`{k}`" for k in r["keywords"]))
        with st.expander("원문 인용"):
            st.markdown(f"> {r['quote']}")
            st.caption(f"p.{r['page']} · {r['problem_id']}")
        if selectable and st.button("이 문제로 주제 만들기", key=f"select_{r['problem_id']}", type="primary"):
            return r["problem_id"]
    return None


def render_result(res: dict, selectable: bool) -> str | None:
    """선택 버튼이 눌린 Open Problem의 problem_id (없으면 None)."""
    chosen = None
    if res["status"] == "ok":
        if res["intro"]:
            st.markdown(res["intro"])
        for i, r in enumerate(res["recommendations"], 1):
            chosen = render_card(i, r, selectable) or chosen
    if res["note"]:
        st.info(res["note"])
    tr = res["trace"]
    with st.expander("처리 과정 (디버그)"):
        st.markdown(f"**검색 질의** · {tr['query']['research_description']}")
        st.markdown("**topics** · " + ", ".join(tr["query"]["topics"]))
        st.markdown(f"**선택된 id** · {tr.get('selected')}  \n**검증 탈락 id** · {tr.get('invalid_ids')}  \n"
                    f"**소요 시간** · {tr['elapsed_sec']}초" + (f"  \n**중단 사유** · {tr['stop_reason']}"
                                                         if tr.get("stop_reason") else ""))
        st.dataframe(tr["candidates"], height=240)
    if res["status"] != "ok":
        st.caption("사이드바의 새 대화에서 관심 분야를 바꿔 다시 시도해 보세요.")
    return chosen


def render_topics(res: dict) -> None:
    if res["status"] != "ok":
        st.warning("연구 주제 후보를 만들지 못했습니다. 다른 Open Problem을 선택해 보세요.")
        return
    if res["intro"]:
        st.markdown(res["intro"])
    st.info("아직 중복 검증 전인 후보입니다. 기존 연구와 겹칠 수 있습니다.")
    for i, t in enumerate(res["topics"], 1):
        with st.container(border=True):
            st.markdown(f"#### {i}. {t['title']}")
            st.markdown(f"**연구 질문** · {t['research_question']}")
            st.markdown(f"**접근 방법** · {t['approach']}")
            st.markdown(f"**기대 기여** · {t['contribution']}")
            st.markdown(f"**수행 가능성** · {t['feasibility']}")
    with st.expander("처리 과정 (디버그)"):
        st.markdown(f"**problem_id** · {res['problem_id']}  \n"
                    f"**LLM이 반환한 후보 수** · {res['trace']['returned_topics']}  \n"
                    f"**소요 시간** · {res['trace']['elapsed_sec']}초")


def profile_message(p: dict) -> str:
    text = f"**연구실** {p['lab_name']}  \n**관심 분야** {p['interests']}"
    if p["extra"]:
        text += f"  \n**추가 정보** {p['extra']}"
    return text


with st.sidebar:
    st.button("새 대화", on_click=reset, use_container_width=True)

st.title("Open Problem 추천")

profile = st.session_state.get("profile")

if profile is None:
    with st.chat_message("assistant"):
        st.markdown("연구실 정보를 알려 주시면 제안 논문 5편의 Open Problems 중 관련 있는 항목을 추천해 드립니다.")
        with st.form("profile_form"):
            lab_name = st.text_input("연구실 이름", placeholder="예: 자연어처리 연구실")
            interests = st.text_area("연구 관심 분야", placeholder="예: LLM 환각, 사실성 검증", height=80)
            extra = st.text_area("추가로 알리고 싶은 정보 (선택)",
                                 placeholder="예: 현재 RAG 기반 QA 시스템을 개발 중이고, 평가 방법론 쪽 주제를 찾고 있음",
                                 height=80)
            submitted = st.form_submit_button("추천 받기", type="primary")
    if submitted:
        if not lab_name.strip() or not interests.strip():
            st.warning("연구실 이름과 연구 관심 분야를 입력해 주세요.")
            st.stop()
        profile = {"lab_name": lab_name.strip(), "interests": interests.strip(), "extra": extra.strip()}
        with st.spinner("관련 Open Problem을 찾는 중..."):
            try:
                result = cached_recommend(profile["lab_name"], profile["interests"], profile["extra"])
            except Exception as e:  # API 오류 등
                st.error(f"추천 중 오류가 발생했습니다: {e}")
                st.stop()
        st.session_state.update(profile=profile, result=result, messages=[])
        st.rerun()
    st.stop()

# 1턴 (프로필 → Open Problem 리스트)
topic_result = st.session_state.get("topic_result")
with st.chat_message("user"):
    st.markdown(profile_message(profile))
with st.chat_message("assistant"):
    chosen = render_result(st.session_state["result"], selectable=topic_result is None)

if chosen:
    with st.spinner("연구 주제 후보를 만드는 중..."):
        try:
            st.session_state["topic_result"] = cached_topics(profile["lab_name"], profile["interests"],
                                                             profile["extra"], chosen)
        except Exception as e:  # API 오류 등
            st.error(f"주제 생성 중 오류가 발생했습니다: {e}")
            st.stop()
    st.rerun()

if topic_result is None:
    st.stop()

# 2턴 (Open Problem 선택 → 연구 주제 후보)
with st.chat_message("user"):
    st.markdown(f"**{topic_result['problem_title']}** 문제로 연구 주제를 만들어 주세요.")
with st.chat_message("assistant"):
    render_topics(topic_result)
    st.button("다른 Open Problem 선택", on_click=choose_other_problem)

# 후속 대화
for m in st.session_state["messages"]:
    with st.chat_message(m["role"]):
        st.markdown(m["content"])

if question := st.chat_input("연구 주제 후보에 대해 질문하기"):
    with st.chat_message("user"):
        st.markdown(question)
    with st.chat_message("assistant"):
        try:
            answer = st.write_stream(reply_stream(profile, st.session_state["result"],
                                                  st.session_state["messages"], question, topic_result))
        except Exception as e:  # API 오류 등
            st.error(f"답변 중 오류가 발생했습니다: {e}")
            st.stop()
    st.session_state["messages"] += [{"role": "user", "content": question},
                                     {"role": "assistant", "content": answer}]
