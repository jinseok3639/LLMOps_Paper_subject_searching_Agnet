/* ④ 검증 화면 (설계서 4장, API 명세 6.4). app.js의 App 훅만 쓴다.
   ③에서 후보를 고르면 "선택한 후보 검증" → /api/verify → picks.verify_run_id, stage "verify".
   ⑤(다른 모듈)는 picks.verify_run_id, research_run_id, topic_id, recommend_run_id, problem_id를 읽는다. */
(() => {
  const {h} = App;
  const VERDICT = {solved: "이미 한 연구가 있음 (solved)", partial: "일부 겹침 (partial)",
                   not_addressed: "같은 문제·방법을 한 논문을 찾지 못함 (not_addressed)"};
  const ADVICE = {
    solved: "한 논문이 문제와 방법을 모두 이미 다룹니다. ③으로 돌아가 다른 후보를 고르거나 후보를 수정하는 것을 권합니다.",
    partial: "겹치는 요소와 다른 요소를 아래 표에서 나눠 확인하세요.",
    not_addressed: "가장 가까운 논문과의 차이를 아래 표에서 확인하세요. 찾은 범위 안의 결과일 뿐 새롭다는 보장은 아닙니다."};
  const FLAG_NOTE = {"초록 수준": "본문을 읽지 못해 초록 카드로 비교한 논문이 있습니다.",
                     "확인 못 한 요소 있음": "카드에 없거나 인용 대조에 실패해 판단하지 못한 문제·방법 요소가 있습니다."};
  const MATCH = {same: "같음", partial: "부분", different: "다름", unknown: "확인 못 함"};
  const ELEMENTS = [["problem", "문제"], ["method", "방법"], ["assumptions", "가정"], ["evaluation", "평가"]];
  const REQUIREMENTS = [["problem_scope", "문제와 범위"], ["difference", "기존 연구와의 차이"], ["mechanism", "핵심 메커니즘"],
                        ["method_definition", "방법 정의"], ["validation_design", "검증 설계"],
                        ["alternative_explanations", "대안 설명 배제"], ["failure_feasibility", "실패 조건과 실행 가능성"]];
  const STATUS = {defined: "명시됨", undecided: "미정", not_applicable: "해당 없음"};
  const REVIEW = [["unsupported", "근거 없는 주장"], ["vague", "검증을 막는 빈 정의·절차"], ["already_done", "이미 한 부분"],
                  ["suggestion", "가장 중요한 개선"]];
  const safeUrl = u => /^https?:\/\//.test(u || "") ? u : null;

  document.head.append(h("style", {}, `
    .vtable { border-collapse: collapse; width: 100%; margin: 6px 0; font-size: 14px; }
    .vtable th, .vtable td { border: 1px solid #ddd; padding: 4px 6px; text-align: left; vertical-align: top; }
    .vtable th { background: #f4f4f6; white-space: nowrap; }`));

  function startVerify(s, {replace} = {}) {
    return App.runTask({kind: "verify", label: "후보 검증", url: "/api/verify", replace,
      body: {profile: s.profile, problem_id: s.picks.problem_id, research_run_id: s.picks.research_run_id,
             topic_id: s.picks.topic_id},
      onResult: (x, d) => { x.picks = {...x.picks, verify_run_id: d.run_id}; x.stage = "verify"; }});
  }

  const page = p => p == null ? "" : ` (p.${p})`;

  function paperLine(p) {
    const url = safeUrl(p.url);
    return h("h4", {}, `[${p.id}] `, url ? h("a", {href: url, target: "_blank", rel: "noopener"}, p.title) : p.title,
             ` (${p.year ?? "연도 없음"}) · ${p.level === "fulltext" ? "본문" : "초록"} 수준`);
  }

  function elementTable(p) {
    return h("table", {class: "vtable"},
      h("tr", {}, h("th", {}, "요소"), h("th", {}, "일치"), h("th", {}, "이유"), h("th", {}, "논문 근거")),
      ELEMENTS.map(([key, label]) => {
        const e = p.comparison[key] || {};
        return h("tr", {}, h("th", {}, label), h("td", {}, MATCH[e.match] || e.match || ""), h("td", {}, e.reason || ""),
                 h("td", {}, e.quote ? `"${e.quote}"${page(e.page)}` : ""));
      }));
  }

  function critique(c) {
    if (!c) return h("p", {class: "caption"}, "비평을 만들지 못했습니다.");
    return h("div", {},
      h("table", {class: "vtable"}, h("tr", {}, h("th", {}, "요건"), h("th", {}, "상태"), h("th", {}, "메모")),
        REQUIREMENTS.map(([key, label]) => {
          const r = c.requirements[key] || {};
          return h("tr", {}, h("th", {}, label), h("td", {}, STATUS[r.status] || r.status || ""), h("td", {}, r.note || ""));
        })),
      h("p", {class: "caption"}, "defined(명시됨)는 명시 여부일 뿐 타당성 보장이 아닙니다. " +
                                 "7요건은 AI 사례 5개로 만든 잠정 기준(v0)입니다."),
      REVIEW.filter(([key]) => c[key]).map(([key, label]) => h("p", {}, h("b", {}, `${label}: `), c[key])));
  }

  function renderVerify(el, s) {
    const run = App.getRun(s.picks.verify_run_id);
    if (!run) return el.append(App.loading(s.picks.verify_run_id),
                               h("p", {class: "caption"}, "결과는 24시간 보관됩니다. 지났으면 ③에서 다시 검증하세요."));
    const sc = run.scope || {}, nearest = run.nearest || [];
    const basis = nearest.filter(p => (run.verdict_basis || []).includes(p.paper_id)).map(p => `[${p.id}] ${p.title}`);
    el.append(h("h2", {}, `④ 검증: ${run.topic_title || ""}`),
      h("div", {class: "info"}, h("b", {}, `판정: ${VERDICT[run.verdict] || run.verdict || "없음"}`),
        h("p", {}, ADVICE[run.verdict] || ""),
        basis.length ? h("p", {}, `근거 논문: ${basis.join(", ")}`) : "",
        (run.flags || []).map(f => h("p", {class: "caption"}, `${f}: ${FLAG_NOTE[f] || ""}`))),
      h("p", {class: "caption"}, `조사 범위: 검색어 ${(sc.queries || []).join(" / ")} · 논문 ${sc.searched ?? 0}편 중 ` +
        `${sc.narrowed ?? 0}편 비교 · 본문까지 읽은 논문 ${sc.fulltext_read ?? 0}편 · ${sc.date || ""}`),
      h("h3", {}, "가장 가까운 논문"),
      ...nearest.map(p => h("div", {class: "card"}, paperLine(p), elementTable(p))),
      h("h3", {}, "빈틈: 논문이 밝힌 한계"),
      (run.limitations || []).length
        ? h("ul", {}, run.limitations.map(l => h("li", {}, `[${l.id}] ${l.title}: "${l.quote}"${page(l.page)}`)))
        : h("p", {class: "caption"}, "가장 가까운 논문의 카드에 저자가 밝힌 한계가 없습니다."),
      h("h3", {}, "비평 (ScholarIdeas 7요건)"),
      critique(run.critique));
  }

  App.registerPanel("verify", {render: renderVerify, reachable: s => !!s.picks.verify_run_id, summary: s => {
    const run = App.getRun(s.picks.verify_run_id);
    return run ? `판정: ${run.verdict}` + ((run.flags || []).length ? ` · ${run.flags.join(", ")}` : "") : "";
  }});
  App.addAction("refine", {label: "선택한 후보 검증", enabled: s => !!(s.picks.topic_id && s.picks.research_run_id),
                           run: s => startVerify(s)});
  App.addAction("verify", {label: "판정 수용 → ⑤ 확정", enabled: s => !!App.getRun(s.picks.verify_run_id),
                           run: () => App.setStage("final")});
  App.addAction("verify", {label: "③으로 돌아가 수정", run: () => App.setStage("refine")});
  App.registerJob("verify", startVerify);
})();
