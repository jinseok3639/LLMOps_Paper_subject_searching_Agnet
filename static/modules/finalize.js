/* ⑤ 확정 화면 (설계서 5장, 요구사항 R-1, R-2). app.js의 App 훅만 쓴다.
   "최종 주제 카드 만들기" → /api/finalize → picks.final_run_id. 카드는 서버가 만든 마크다운을 그대로 보여 주고,
   "마크다운으로 내보내기"는 같은 문자열을 브라우저에서 파일로 내려받는다. 검증 없이 만들면 판정은 "검증 안 함". */
(() => {
  const {h} = App;
  const PICKS = ["recommend_run_id", "problem_id", "research_run_id", "topic_id", "verify_run_id"];
  // 카드를 만든 뒤 후보를 바꾸거나 다시 검증했으면 카드가 낡은 것이다
  const stale = (s, run) => ["research_run_id", "topic_id", "verify_run_id"]
    .some(k => (run.card.source?.[k] || "") !== (s.picks[k] || ""));

  document.head.append(h("style", {}, `
    .final-card table { border-collapse: collapse; width: 100%; margin: 6px 0; font-size: 14px; }
    .final-card th, .final-card td { border: 1px solid #ddd; padding: 4px 6px; text-align: left; vertical-align: top; }
    .final-card th { background: #f4f4f6; }`));

  function startFinalize(s, {replace} = {}) {
    return App.runTask({kind: "finalize", label: "최종 주제 카드 만들기", url: "/api/finalize", replace,
      body: Object.fromEntries(PICKS.map(k => [k, s.picks[k] || ""])),
      onResult: (x, d) => { x.picks = {...x.picks, final_run_id: d.run_id}; x.stage = "final"; }});
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

  function renderFinal(el, s) {
    const id = s.picks.final_run_id, run = App.getRun(id);
    const make = label => h("button", {class: "primary", onclick: () => startFinalize(s)}, label);
    el.append(h("h2", {}, "⑤ 확정: 최종 주제 카드"));
    if (!id) return el.append(
      h("p", {class: "caption"}, s.picks.verify_run_id
        ? "저장된 추천·후보·검증 결과로 카드를 만듭니다 (LLM 호출 없음)."
        : "검증 결과가 없어 판정은 \"검증 안 함\"으로 들어갑니다. ③에서 \"선택한 후보 검증\"을 먼저 할 수 있습니다."),
      make("최종 주제 카드 만들기"));
    if (!run) return el.append(h("p", {class: "caption"}, "카드를 불러오는 중... 오래 걸리면 보관 기간(24시간)이 " +
                                                         "지났을 수 있으니 다시 만드세요."), make("다시 만들기"));
    if (stale(s, run)) el.append(h("p", {class: "info"}, "카드를 만든 뒤 후보나 검증 결과가 바뀌었습니다. ",
                                   make("현재 결과로 다시 만들기")));
    const body = h("div", {class: "md card final-card"});
    body.innerHTML = App.md(run.markdown);
    el.append(h("div", {class: "row"}, h("button", {onclick: () => download(run)}, "마크다운으로 내보내기")), body);
  }

  App.registerPanel("final", {render: renderFinal,
    reachable: s => !!(s.picks.final_run_id || (s.picks.research_run_id && s.picks.topic_id)),
    summary: s => {
      const run = App.getRun(s.picks.final_run_id);
      return run ? `카드 완성 · 판정: ${run.card.verdict.label}` : "";
    }});
  App.registerJob("finalize", startFinalize);
})();
