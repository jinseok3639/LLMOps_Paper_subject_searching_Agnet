/* 좋아요/싫어요 (요구사항 O-3, API 명세 6.5). App.addCardExtra로 ① 추천 카드(target: problem)와
   ③ 후보 카드(target: topic)에 버튼을 단다. 보낸 값은 세션의 feedback[`${target}:${target_id}`]에 남겨
   다시 그릴 때 고른 버튼을 표시한다. 후보 id는 작업마다 다시 쓰이므로 target_id 앞에 카드가 든 메시지의 run_id(research_run_id)를 붙인다. */
(() => {
  const {h} = App;
  const TARGETS = {problem: item => item.problem_id, topic: (item, s, m) => `${m.run_id}:${item.id}`};

  async function send(s, target, target_id, value) {
    const res = await fetch("/api/feedback", {method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({session_id: s.id, target, target_id, value})}).catch(() => null);
    if (!res || !res.ok) return alert("피드백을 보내지 못했습니다. 잠시 후 다시 눌러 주세요.");
    App.updateSession(s.id, x => { (x.feedback ||= {})[`${target}:${target_id}`] = value; });
  }

  const THUMB = '<svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="2" ' +
                'stroke-linecap="round" stroke-linejoin="round"><path d="M7 10v11"/><path d="M15 5.9 14 10h5.8a2 2 0 0 1 2 2.3' +
                'l-1.4 7A2 2 0 0 1 18.4 21H4a1 1 0 0 1-1-1v-9a1 1 0 0 1 1-1h2.8a2 2 0 0 0 1.8-1.1L12 2a3.1 3.1 0 0 1 3 3.9Z"/></svg>';

  App.addCardExtra((kind, s, item, m) => {
    if (!TARGETS[kind]) return null;
    const id = TARGETS[kind](item, s, m), chosen = (s.feedback || {})[`${kind}:${id}`];
    const button = (value, title) => {
      const b = h("button", {type: "button", class: `icon-btn${value === "down" ? " flip" : ""}${chosen === value ? " on" : ""}`,
        title, "aria-label": title, "aria-pressed": String(chosen === value),
        onclick: () => { if (chosen !== value) send(s, kind, id, value); }});
      b.innerHTML = THUMB;
      return b;
    };
    return h("div", {class: "feedback"}, button("up", "좋아요"), button("down", "싫어요"));
  });
})();
