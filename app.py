"""Research assistant: profile -> browse -> bibliography agents -> refinement."""
import streamlit as st
from src.chat import reply_stream
from src.recommend import recommend
from src.research import generate_research

st.set_page_config(page_title="연구 주제 탐색", page_icon="🔎", layout="wide")
STATES = [("browse", "① 둘러보기"), ("explore", "② 문제 파고들기"),
          ("refine", "③ 주제 다듬기"), ("verify", "④ 검증"), ("final", "⑤ 확정")]


def reset():
    for key in ("profile", "result", "messages", "topic_result", "problem", "stage", "selected_topic", "activity"):
        st.session_state.pop(key, None)


def change_stage(stage):
    st.session_state.stage = stage


def choose_problem(problem):
    st.session_state.problem = problem
    st.session_state.pop("topic_result", None)
    st.session_state.pop("selected_topic", None)
    change_stage("explore")


def browse():
    st.session_state.pop("problem", None)
    st.session_state.pop("topic_result", None)
    st.session_state.pop("selected_topic", None)
    change_stage("browse")


st.session_state.setdefault("stage", "browse")
st.session_state.setdefault("messages", [])
main, right = st.columns([4, 1.3], gap="large")
with right:
    panel = st.empty()


def render_state(activity=""):
    with panel.container(border=True):
        st.markdown("### 현재 상태")
        for key, label in STATES:
            if key in ("verify", "final"):
                st.caption(f"○ {label} · 준비 중")
            elif key == st.session_state.stage:
                st.success(f"▶ {label}")
            else:
                st.write(label)
        st.caption("문제 선택 → 탐색 / 후보 생성 완료 → 다듬기")
        st.caption("중복 판정은 이번 버전에서 실행하지 않습니다.")
        if activity:
            st.info(activity)


render_state()
with main:
    st.title("연구 주제 탐색")
    if st.button("새 대화"):
        reset()
        st.rerun()
    if "profile" not in st.session_state:
        with st.form("profile"):
            lab = st.text_input("연구실 이름")
            interests = st.text_area("관심 분야")
            extra = st.text_area("보유 자원·기간·제약·추가 정보")
            submitted = st.form_submit_button("문제 추천 받기", type="primary")
        if submitted:
            if not lab.strip() or not interests.strip():
                st.warning("연구실 이름과 관심 분야를 입력해 주세요.")
            else:
                profile = {"lab_name": lab.strip(), "interests": interests.strip(), "extra": extra.strip()}
                try:
                    render_state("프로필에 맞는 문제를 추천하는 중")
                    result = recommend(profile["lab_name"], profile["interests"], profile["extra"])
                    st.session_state.update(profile=profile, result=result)
                    st.rerun()
                except Exception as exc:
                    render_state()
                    st.error(f"추천 실패: {exc}")
        st.stop()

    profile = st.session_state.profile
    result = st.session_state.result
    st.caption(f"{profile['lab_name']} · {profile['interests']}")
    if st.session_state.stage == "browse":
        if result["intro"]:
            st.markdown(result["intro"])
        for i, card in enumerate(result["recommendations"], 1):
            with st.container(border=True):
                st.subheader(f"{i}. {card['title']}")
                st.write(card["summary"])
                st.write(card["reason"])
                st.caption(f"출처: {card['paper']['title']} · p.{card['page']}")
                with st.expander("원문 근거"):
                    st.write(card["quote"])
                if st.button("이 문제 파고들기", key=card["problem_id"]):
                    choose_problem(card)
                    st.rerun()
        if result["note"]:
            st.info(result["note"])
    else:
        st.subheader(st.session_state.problem["title"])
        if st.button("다른 문제 둘러보기"):
            browse()
            st.rerun()
        st.caption("대화로 조건을 더한 뒤 생성 버튼을 누르면 프로필·대화·선택한 문제를 바탕으로 참고문헌을 검색합니다.")
        if st.button("참고문헌 검색하고 후보 생성" if "topic_result" not in st.session_state else "현재 대화를 반영해 다시 검색·생성", type="primary"):
            previous_stage = st.session_state.stage
            try:
                with st.status("검색 에이전트 → 후보 생성 에이전트 → 대화 에이전트", expanded=True) as status:
                    def progress(message):
                        render_state(message)
                        status.write(message)
                    new_result = generate_research(profile, list(st.session_state.messages),
                                                   st.session_state.problem, progress)
                    st.session_state.topic_result = new_result
                    st.session_state.pop("selected_topic", None)
                    change_stage("refine")
                    status.update(label="후보 생성 완료", state="complete")
                st.rerun()
            except Exception as exc:
                change_stage(previous_stage)
                render_state()
                st.error(f"검색·생성 실패: {exc}")

    topic_result = st.session_state.get("topic_result")
    if topic_result:
        with st.chat_message("assistant"):
            st.markdown(topic_result["presentation"])
        with st.expander(f"검색 참고문헌 {len(topic_result['references'])}편 · 검색 과정"):
            st.json(topic_result["trace"])
            for ref in topic_result["references"]:
                st.write(f"[{ref['id']}] {ref['title']} ({ref['year']})")
                if ref["url"]:
                    st.link_button("논문 보기", ref["url"])
        ids = [t["id"] for t in topic_result["topics"]]
        by_id = {t["id"]: t for t in topic_result["topics"]}
        selected = st.selectbox("다듬을 후보 선택", ids, index=None,
                                format_func=lambda cid: by_id[cid]["title"],
                                key=f"choice_{topic_result['run_id']}")
        if selected and selected != st.session_state.get("selected_topic"):
            st.session_state.selected_topic = selected
            st.session_state.messages.append({"role": "user", "content": f"이 후보를 다듬겠습니다: {by_id[selected]['title']}"})
            st.rerun()
    for message in st.session_state.messages:
        with st.chat_message(message["role"]):
            st.markdown(message["content"])
    if question := st.chat_input("질문하거나 연구 조건을 알려 주세요"):
        with st.chat_message("user"):
            st.write(question)
        try:
            with st.chat_message("assistant"):
                answer = st.write_stream(reply_stream(profile, result, st.session_state.messages,
                                                      question, topic_result))
            st.session_state.messages.extend([{"role": "user", "content": question},
                                               {"role": "assistant", "content": answer}])
        except Exception as exc:
            st.error(f"대화 실패: {exc}")
