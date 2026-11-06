/* ③·④ 후보 고치기 (이슈 #12). app.js의 App 훅만 쓴다.
   채팅으로 "방법을 ~로 바꿔 줘"(revise_topic) 또는 ④ 카드의 "결과를 반영해 고치기" → /api/revise → kind "revision" 메시지.
   수정안 카드는 바뀐 필드만 보이고 [적용] · [다시 말하기]를 단다. 적용하면 picks.revision_run_id가 이 수정안을 가리키고
   버전이 오른다 (v1 → v2). 검증을 다시 하라고 강제하지 않는다: 적용한 카드에 "vN 검증"과, 검증한 적이 있으면 "이대로 확정"을 단다.
   메시지 필드: run_id, research_run_id, topic_id, version, applied | rejected. */
(() => {
  const {h, btn} = App;
  const LABEL = {title: "제목", summary: "요약", gap: "연구 공백 (gap)", approach: "방법 (approach)", evaluation: "평가 (evaluation)"};

  function startRevise(s, {replace} = {}) {
    // 검증한 적이 있으면 그 결과도 넘긴다. 결과를 반영할지는 사용자 말에 따라 서버 프롬프트가 정한다
    const {research_run_id, topic_id} = s.picks;
    return App.runTask({kind: "revise", label: "후보 수정안 만들기", url: "/api/revise", replace,
      body: {profile: s.profile, research_run_id, topic_id, revision_run_id: s.picks.revision_run_id || "",
             verify_run_id: s.picks.verify_run_id || "", messages: s.messages},
      onResult: (x, d) => {
        x.messages.forEach(y => { if (y.kind === "revision" && !y.applied) y.rejected = true; });  // 새 수정안이 앞선 안을 대신한다
        if (!d.run_id)
          return App.msg("assistant", (d.reason ? d.reason + "\n\n" : "") +
                         "고칠 내용을 찾지 못해 후보를 그대로 두었습니다. 무엇을 어떻게 바꿀지 입력창에 알려 주세요.");
        const fields = d.changes.map(c => LABEL[c.field] || c.field).join(", ");
        return App.msg("assistant", `후보 수정안 v${d.version}: ${fields} 변경`,
                       {kind: "revision", run_id: d.run_id, research_run_id, topic_id, version: d.version});
      }});
  }

  function apply(s, m) {
    App.updateSession(s.id, x => {
      const y = x.messages.find(z => z.id === m.id);
      if (y) y.applied = true;
      x.picks = {...x.picks, revision_run_id: m.run_id};  // 검증 결과(verify_run_id)는 남긴다. 이전 판정으로 확정해도 된다
      delete x.picks.final_run_id;
      x.stage = "refine";
    });
  }

  function reject(s, m) {
    App.updateSession(s.id, x => { const y = x.messages.find(z => z.id === m.id); if (y) y.rejected = true; });
    document.querySelector("#chat-input")?.focus();
  }

  function verifyNow(s, m) {
    App.updateSession(s.id, x => { x.messages.push(App.msg("user", `v${m.version}로 검증해 주세요.`)); });
    App.startJob("verify", App.getSession());
  }

  function finalizeNow(s) {
    App.updateSession(s.id, x => { x.messages.push(App.msg("user", "이대로 확정할게요.")); });
    App.startJob("finalize", App.getSession());
  }

  function renderRevision(el, s, m, active) {
    const run = App.getRun(m.run_id);
    if (!run) return el.append(App.loading(m.run_id));
    const state = m.applied ? "applied" : m.rejected ? "rejected" : "proposed";
    el.append(
      h("p", {}, state === "proposed" ? `후보를 이렇게 고칠까요? (v${run.base_version} → v${run.version})`
        : state === "applied" ? `v${run.version}로 고쳤습니다.` : "이 수정안은 적용하지 않았습니다."),
      run.reason ? h("p", {class: "reason"}, run.reason) : "",
      h("div", {class: "card" + (state === "applied" ? " picked" : "")},
        h("div", {class: "card-title"}, run.topic.title,
          state === "applied" ? h("span", {class: "badge"}, `v${run.version} 적용함`) : "",
          state === "rejected" ? h("span", {class: "badge gray"}, "적용 안 함") : ""),
        h("table", {class: "vtable"}, h("tr", {}, h("th", {}, "항목"), h("th", {}, "지금"), h("th", {}, "고친 뒤")),
          run.changes.map(c => h("tr", {}, h("th", {}, LABEL[c.field] || c.field),
                                 h("td", {class: "before"}, c.before_ko || c.before || "(비어 있음)"),
                                 h("td", {}, c.after_ko || c.after)))),
        h("details", {}, h("summary", {}, "고친 뒤 전체 후보"),
          Object.keys(LABEL).filter(k => run.topic[k]).map(k => h("p", {}, h("b", {}, `${LABEL[k]}: `), App.ko(run.topic, k))))));
    if (!active) return;
    if (state === "proposed")
      return el.append(h("div", {class: "row"}, btn("적용", () => apply(s, m), "primary"), btn("다시 말하기", () => reject(s, m))),
                       h("p", {class: "caption"}, "다르게 고치고 싶으면 \"다시 말하기\"를 누르고 입력창에 바꿀 내용을 알려 주세요."));
    if (state === "rejected")
      return el.append(h("p", {class: "caption"}, "바꿀 내용을 입력창에 다시 알려 주세요."));
    el.append(h("div", {class: "row"}, btn(`v${run.version} 검증`, () => verifyNow(s, m), "primary"),
                s.picks.verify_run_id ? btn("이대로 확정", () => finalizeNow(s)) : ""),
              h("p", {class: "caption"}, s.picks.verify_run_id
                ? "고친 내용은 아직 검증하지 않았습니다. 이대로 확정하면 이전 버전의 판정이 카드에 들어갑니다."
                : "더 고칠 내용이 있으면 입력창에 알려 주세요."));
  }

  App.registerCard("revision", renderRevision);
  App.registerJob("revise", startRevise);
  App.registerActions("revision", (s, m) => {
    if (!m.applied && !m.rejected)
      return [{name: "apply_revision", target: "", label: "보여 준 수정안 적용"},
              {name: "revise_topic", target: "", label: "수정안을 다르게 고치기: 사용자가 말한 변경을 다시 반영"}];
    return [{name: "verify_topic", target: "", label: "지금 버전의 후보 검증"},
            {name: "revise_topic", target: "", label: "후보를 더 고치기: 사용자가 말한 변경을 반영"},
            ...(s.picks.verify_run_id ? [{name: "finalize", target: "", label: "이대로 최종 주제 카드로 확정"}] : [])];
  });
  App.registerAction("apply_revision", s => {
    const m = s.messages.findLast(x => x.kind === "revision");
    if (m && !m.applied && !m.rejected) apply(s, m);
  });
})();
