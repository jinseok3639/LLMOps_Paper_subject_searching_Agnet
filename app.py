"""Open Problem 추천 챗봇.

첫 턴: 연구실 프로필 입력 → 추천 카드. 이후: 추천 결과를 근거로 후속 대화 (도구 연결 없음).
실행: streamlit run app.py
"""
import streamlit as st

from src.chat import reply_stream
from src.recommend import recommend

st.set_page_config(page_title="Open Problem 추천", page_icon="🔎")


@st.cache_data(show_spinner=False)
def cached_recommend(lab_name: str, interests: str, extra: str) -> dict:
    return recommend(lab_name, interests, extra)


def reset() -> None:
    for key in ("profile", "result", "messages"):
        st.session_state.pop(key, None)


def render_card(i: int, r: dict) -> None:
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


def render_result(res: dict) -> None:
    if res["status"] == "ok":
        if res["intro"]:
            st.markdown(res["intro"])
        for i, r in enumerate(res["recommendations"], 1):
            render_card(i, r)
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
    st.caption("추천된 Open Problem에 대해 더 물어보세요. 예: 1번을 석사 과제로 구체화해 줘 / 다른 후보도 보여 줘")


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

# 첫 턴 (프로필 → 추천)
with st.chat_message("user"):
    st.markdown(profile_message(profile))
with st.chat_message("assistant"):
    render_result(st.session_state["result"])

# 후속 대화
for m in st.session_state["messages"]:
    with st.chat_message(m["role"]):
        st.markdown(m["content"])

if question := st.chat_input("추천 결과에 대해 질문하기"):
    with st.chat_message("user"):
        st.markdown(question)
    with st.chat_message("assistant"):
        try:
            answer = st.write_stream(reply_stream(profile, st.session_state["result"],
                                                  st.session_state["messages"], question))
        except Exception as e:  # API 오류 등
            st.error(f"답변 중 오류가 발생했습니다: {e}")
            st.stop()
    st.session_state["messages"] += [{"role": "user", "content": question},
                                     {"role": "assistant", "content": answer}]
