/* 좋아요/싫어요 (요구사항 O-3, API 명세 6.5). App.addCardExtra로 ① 추천 카드(target: problem)와
   ③ 후보 카드(target: topic)에 버튼을 단다. 보낸 값은 세션의 feedback[`${target}:${target_id}`]에 남겨
   다시 그릴 때 고른 버튼을 표시한다. 후보 id는 작업마다 다시 쓰이므로 target_id 앞에 research_run_id를 붙인다. */
(() => {
  const {h} = App;
  const TARGETS = {problem: item => item.problem_id, topic: (item, s) => `${s.picks.research_run_id}:${item.id}`};

  async function send(s, target, target_id, value) {
    const res = await fetch("/api/feedback", {method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({session_id: s.id, target, target_id, value})}).catch(() => null);
    if (!res || !res.ok) return alert("피드백을 보내지 못했습니다. 잠시 후 다시 눌러 주세요.");
    App.updateSession(s.id, x => { (x.feedback ||= {})[`${target}:${target_id}`] = value; });
  }

  App.addCardExtra((kind, s, item) => {
    if (!TARGETS[kind]) return null;
    const id = TARGETS[kind](item, s), chosen = (s.feedback || {})[`${kind}:${id}`];
    const button = (value, label, title) => h("button", {
      class: chosen === value ? "primary" : "", title, "aria-pressed": String(chosen === value),
      onclick: () => { if (chosen !== value) send(s, kind, id, value); }}, label);
    return h("div", {class: "row"}, button("up", "👍", "좋아요"), button("down", "👎", "싫어요"));
  });
})();
