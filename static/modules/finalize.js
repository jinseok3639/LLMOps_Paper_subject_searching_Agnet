/* ⑤ 확정 메시지 (설계서 5장, 요구사항 R-1, R-2). app.js의 App 훅만 쓴다.
   ④ 검증 카드의 "이 주제로 확정" → /api/finalize → 대화에 kind "final" 메시지, picks.final_run_id, stage "final".
   카드는 서버가 만든 마크다운을 그대로 보여 주고, "마크다운으로 내보내기"는 같은 문자열을 브라우저에서 파일로 내려받는다.
   내보내기는 다음 단계로 가는 버튼이 아니라서 지나간 카드에서도 누를 수 있다. */
(() => {
  const {h, btn} = App;
  const PICKS = ["recommend_run_id", "problem_id", "research_run_id", "topic_id", "revision_run_id", "verify_run_id"];

  function startFinalize(s, {replace} = {}) {
    return App.runTask({kind: "finalize", label: "최종 주제 카드 만들기", url: "/api/finalize", replace,
      body: Object.fromEntries(PICKS.map(k => [k, s.picks[k] || ""])),
      onResult: (x, d) => {
        x.picks = {...x.picks, final_run_id: d.run_id};
        x.stage = "final";
        return App.msg("assistant", `최종 주제 카드: ${d.card?.topic?.title || ""}`, {kind: "final", run_id: d.run_id});
      }});
  }

  function download(run) {
    const url = URL.createObjectURL(new Blob([run.markdown], {type: "text/markdown;charset=utf-8"}));
    const name = (run.card.topic.title || "topic").replace(/[\\/:*?"<>|\s]+/g, " ").trim().slice(0, 80) || "topic";
    const a = h("a", {href: url, download: `${name}.md`});
    document.body.append(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }

  function renderFinal(el, s, m) {
    const run = App.getRun(m.run_id);
    if (!run) return el.append(App.loading(m.run_id));
    const body = h("div", {class: "md card final-card"});
    body.innerHTML = App.md(run.markdown);
    el.append(h("p", {}, "최종 주제 카드를 만들었습니다. 내보내서 보관하거나, 궁금한 점은 이어서 물어보세요."), body,
              h("div", {class: "row"}, btn("마크다운으로 내보내기", () => download(run), "primary")));
  }

  App.registerCard("final", renderFinal);
  App.registerStage("final", s => {
    const run = App.getRun(s.picks.final_run_id);
    return run ? `카드 완성 · 판정: ${run.card.verdict.label}` : "";
  });
  App.registerJob("finalize", startFinalize);
})();
