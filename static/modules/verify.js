/* ④ 검증 메시지 (설계서 4장, API 명세 6.4). app.js의 App 훅만 쓴다.
   ③ 후보 카드의 "이 후보 검증" → /api/verify → 대화에 kind "verify" 메시지, picks.verify_run_id, stage "verify".
   다음 단계 버튼: "이 주제로 확정"(finalize 작업) · "다른 후보 검증"(후보 카드를 대화 아래에 다시 보인다).
   ⑤(다른 모듈)는 picks.verify_run_id, research_run_id, topic_id, recommend_run_id, problem_id를 읽는다. */
(() => {
  const {h, btn} = App;
  const VERDICT = {solved: "이미 한 연구가 있음 (solved)", partial: "일부 겹침 (partial)",
                   not_addressed: "같은 문제·방법을 한 논문을 찾지 못함 (not_addressed)"};
  const ADVICE = {
    solved: "한 논문이 문제와 방법을 모두 이미 다룹니다. 다른 후보를 검증하거나, 입력창으로 후보 수정을 요청하는 것을 권합니다.",
    partial: "겹치는 요소와 다른 요소를 아래 표에서 나눠 확인하세요.",
    not_addressed: "가장 가까운 논문과의 차이를 아래 표에서 확인하세요. 찾은 범위 안의 결과일 뿐 새롭다는 보장은 아닙니다."};
  const FLAG_NOTE = {"초록 수준": "본문을 읽지 못해 초록 카드로 비교한 논문이 있습니다. " +
                                "초록에 없는 방법·평가 세부는 비교하지 못했으니 원문 링크에서 확인하세요.",
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
  // fulltext_status → 본문을 못 읽은 이유 (src/finalize.py UNREAD와 같게 맞춘다)
  const UNREAD = {"no_pdf: s2_failed": "논문 정보 조회 실패", "no_pdf: no_url": "공개된 PDF 없음",
                  "no_pdf: not_pdf": "PDF 대신 웹페이지가 옴", "no_pdf: download_failed": "PDF 내려받기 실패",
                  "no_pdf": "PDF를 받지 못함", studio_unavailable: "본문 분석 설정 없음", studio_error: "본문 분석 실패"};
  const unreadReason = s => !s || s === "ok" ? "" : UNREAD[s] || UNREAD[s.split(":")[0]] || "";

  function startVerify(s, {replace} = {}) {
    const {research_run_id, topic_id} = s.picks;
    return App.runTask({kind: "verify", label: "후보 검증", url: "/api/verify", replace,
      body: {profile: s.profile, problem_id: s.picks.problem_id, research_run_id, topic_id},
      onResult: (x, d) => {
        x.picks = {...x.picks, verify_run_id: d.run_id};
        delete x.picks.final_run_id;
        x.stage = "verify";
        return App.msg("assistant", `검증 판정: ${VERDICT[d.verdict] || d.verdict || "없음"} (${d.topic_title || topic_id})`,
                       {kind: "verify", run_id: d.run_id, research_run_id, topic_id});
      }});
  }

  function finalize(s) {
    App.updateSession(s.id, x => { x.messages.push(App.msg("user", "이 주제로 확정할게요.")); });
    App.startJob("finalize", App.getSession());
  }

  function otherTopic(s, m) {  // 되돌아가지 않고 후보 카드를 대화 아래에 다시 보인다 (이슈 #15)
    App.updateSession(s.id, x => {
      x.messages.push(App.msg("user", "다른 후보를 검증하고 싶어요."));
      x.messages.push(App.msg("assistant", "검증할 후보를 골라 주세요. 이미 검증한 후보에는 \"검증함\"이 붙어 있습니다.",
                              {kind: "topics", run_id: m.research_run_id || x.picks.research_run_id, repost: true}));
      x.stage = "refine";
    });
  }

  const page = p => p == null ? "" : ` (p.${p})`;

  function paperLine(p) {
    const url = safeUrl(p.url);
    return h("h4", {}, `[${p.id}] `, url ? h("a", {href: url, target: "_blank", rel: "noopener"}, p.title) : p.title,
             ` (${p.year ?? "연도 없음"}) · ${p.level === "fulltext" ? "본문" : "초록"} 수준`,
             p.level !== "fulltext" && unreadReason(p.fulltext_status)
               ? h("span", {class: "caption"}, ` (본문 못 읽음: ${unreadReason(p.fulltext_status)})`) : "");
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

  function renderVerify(el, s, m, active) {
    const run = App.getRun(m.run_id);
    if (!run) return el.append(App.loading(m.run_id));
    const sc = run.scope || {}, nearest = run.nearest || [];
    const basis = nearest.filter(p => (run.verdict_basis || []).includes(p.paper_id)).map(p => `[${p.id}] ${p.title}`);
    const fold = (title, ...kids) => h("details", {class: "section"}, h("summary", {}, title), ...kids);
    el.append(h("p", {}, "검증을 마쳤습니다: ", h("b", {}, run.topic_title || "")),
      h("div", {class: `verdict ${run.verdict || ""}`}, h("div", {class: "verdict-label"}, VERDICT[run.verdict] || run.verdict || "판정 없음"),
        h("p", {}, ADVICE[run.verdict] || ""),
        basis.length ? h("p", {class: "caption"}, `근거 논문: ${basis.join(", ")}`) : "",
        (run.flags || []).map(f => h("p", {class: "caption"}, `${f}: ${FLAG_NOTE[f] || ""}`))),
      h("p", {class: "caption"}, `조사 범위: 검색어 ${(sc.queries || []).join(" / ")} · 논문 ${sc.searched ?? 0}편 중 ` +
        `${sc.narrowed ?? 0}편 비교 · 본문까지 읽은 논문 ${sc.fulltext_read ?? 0}편 · ${sc.date || ""}`),
      fold(`가장 가까운 논문 ${nearest.length}편`, ...nearest.map(p => h("div", {class: "card quiet"}, paperLine(p), elementTable(p)))),
      fold("빈틈: 논문이 밝힌 한계", (run.limitations || []).length
        ? h("ul", {}, run.limitations.map(l => h("li", {}, `[${l.id}] ${l.title}: "${l.quote}"${page(l.page)}`)))
        : h("p", {class: "caption"}, "가장 가까운 논문의 카드에 저자가 밝힌 한계가 없습니다.")),
      fold("비평 (ScholarIdeas 7요건)", critique(run.critique)));
    if (!active) return;
    const solved = run.verdict === "solved";
    el.append(h("div", {class: "row"},
      btn(solved ? "그래도 이 주제로 확정" : "이 주제로 확정", () => finalize(s), solved ? "" : "primary"),
      btn("다른 후보 검증", () => otherTopic(s, m), solved ? "primary" : "")),
      h("p", {class: "caption"}, "후보를 고치고 싶으면 입력창에 요청하세요."));
  }

  App.registerCard("verify", renderVerify);
  App.registerStage("verify", s => {
    const run = App.getRun(s.picks.verify_run_id);
    return run ? `판정: ${run.verdict}` + ((run.flags || []).length ? ` · ${run.flags.join(", ")}` : "") : "";
  });
  App.registerJob("verify", startVerify);
})();
