/* 연구 주제 탐색 화면. 비로그인 세션은 sessionStorage에 API 명세 2.2 형식으로 저장한다.
   메시지에는 재시도를 위해 job(작업 종류)과 note(중단·실패 안내)가 더 붙을 수 있다.

   확장 모듈 (static/modules/<기능>.js, app.js 다음에 실행되고 첫 화면은 DOMContentLoaded 때 그린다):
   App.registerPanel(stage, {render(el, s), summary(s), reachable(s)})
       stage(verify, final 등)의 가운데 화면. summary는 오른쪽 상태 패널의 한 줄, reachable은 눌러서 갈 수 있는지.
       등록하지 않은 stage는 "준비 중"으로 보인다.
   App.addAction(stage, {label, enabled(s), run(s)})   그 stage 화면 아래에 버튼을 더한다 (예: refine에 "검증").
   App.registerJob(kind, start(s, {replace}))          "재시도" 버튼이 부를 시작 함수.
   App.addCardExtra(fn(kind, s, item) -> Node|null)    카드 끝에 붙일 요소 (예: 피드백 버튼).
       kind: "problem"(① 추천 카드, item = 추천 항목) · "topic"(③ 후보 카드, item = 후보).
   App.runTask({kind, label, url, body, stream, replace, onResult(s, data)}) -> Promise<"result"|"cancelled"|"error"|"budget">
       SSE 작업 하나를 돌린다. body에 session_id를 붙이고, 끝날 때까지 그 세션의 입력을 잠근다 (G-2).
       다른 세션으로 가면 abort하고 시작한 세션에 "중단됨" 메시지(받은 부분 응답 포함)를 남긴다 (G-1, G-3, G-4).
       onResult는 끝까지 성공했을 때만 시작한 세션 객체로 불린다. stage 변경은 여기서 한다 (G-6).
       onResult가 {role, content} 메시지를 돌려주면 대화에 넣는다. replace는 재시도로 대신할 메시지 id.
   App.getSession() / App.updateSession(id, fn) / App.setStage(stage) / App.msg(role, content)
   App.getRun(run_id)   /api/runs 결과 캐시. 처음에는 undefined를 돌려주고, 불러오면 다시 그린다.
   App.loading(run_id)  getRun이 undefined일 때 보여 줄 요소. 불러오는 중이면 안내, 실패했으면 오류와 "다시 불러오기".
   App.isBusy() / App.render() / App.h(tag, attrs, ...children) / App.md(text)
*/
(() => {
  const STAGES = [["browse", "① 둘러보기"], ["explore", "② 문제 파고들기"], ["refine", "③ 주제 다듬기"],
                  ["verify", "④ 검증"], ["final", "⑤ 확정"]];
  const FIELDS = [["lab_name", "연구실 정보", true, "예: 보안 신뢰 컴퓨팅 연구실"],
                  ["lab_topics", "연구실 연구 주제", true, "예: AI 보안, 네트워크·시스템 보안, 트래픽 분석"],
                  ["interests", "사용자 관심사", false, "예: LLM 에이전트 프롬프트 인젝션 방어 (선택)"]];
  const PICK_ORDER = ["recommend_run_id", "problem_id", "research_run_id", "topic_id"];
  const DEFAULT_TITLE = "새 대화";
  const KEY = "rta.sessions", CUR = "rta.current", LAST = "rta.last_profile";

  let sessions = JSON.parse(sessionStorage.getItem(KEY) || "[]");
  let currentId = sessionStorage.getItem(CUR);
  let job = null;  // 도는 작업은 늘 화면의 세션 것 하나뿐이다 (세션을 옮기면 취소되므로)
  const runCache = {}, panels = {}, actions = {}, jobs = {}, drafts = {}, formErrors = {};

  // ---------- 도구 ----------
  const $ = sel => document.querySelector(sel);
  const now = () => new Date().toISOString();
  const rid = p => p + Array.from(crypto.getRandomValues(new Uint8Array(8)), b => b.toString(16).padStart(2, "0")).join("");
  const safeUrl = u => /^https?:\/\//.test(u || "") ? u : null;
  const esc = t => String(t ?? "").replace(/[&<>"']/g, c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));

  function h(tag, attrs = {}, ...kids) {
    const el = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs || {})) {
      if (v == null || v === false) continue;
      if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
      else if (k === "class") el.className = v;
      else if (k in el) el[k] = v;
      else el.setAttribute(k, v);
    }
    for (const c of kids.flat()) if (c != null && c !== "" && c !== false) el.append(c instanceof Node ? c : String(c));
    return el;
  }

  function md(text) {  // CDN을 못 불러오면 글자 그대로 보여 준다
    if (window.marked && window.DOMPurify) return DOMPurify.sanitize(marked.parse(text || ""));
    return esc(text).replace(/\n/g, "<br>");
  }
  function mdBlock(text, id) { const d = h("div", {class: "md", id}); d.innerHTML = md(text); return d; }

  // ---------- 세션 (sessionStorage) ----------
  function persist() { sessionStorage.setItem(KEY, JSON.stringify(sessions)); sessionStorage.setItem(CUR, currentId || ""); }
  const find = id => sessions.find(s => s.id === id);
  const getSession = () => find(currentId);

  function updateSession(id, fn) {  // 결과는 항상 id로 찾은 세션에만 쓴다 (G-1). 지워진 세션이면 버린다 (G-7)
    const s = find(id);
    if (!s) return;
    fn(s);
    s.updated_at = now();
    persist();
    render();
  }

  function newSession() {
    const s = {id: rid("s_"), title: DEFAULT_TITLE, created_at: now(), updated_at: now(), profile: null,
               stage: "browse", picks: {}, messages: [], rejections: []};
    sessions.unshift(s);
    return s;
  }

  const msg = (role, content, extra = {}) => ({id: rid("m_"), role, content, status: "done", created_at: now(), ...extra});

  function place(s, m, replace) {  // replace가 있으면 그 자리에 넣고(m이 없으면 지우고), 없으면 끝에 붙인다
    const i = replace ? s.messages.findIndex(x => x.id === replace) : -1;
    if (i >= 0) s.messages.splice(i, 1, ...(m ? [m] : []));
    else if (m) s.messages.push(m);
  }

  function keepPicks(s, from, extra) {  // from 앞의 선택만 남기고 extra를 더한다. 앞 단계를 바꾸면 뒤 결과는 지운다
    const keys = PICK_ORDER.slice(0, PICK_ORDER.indexOf(from)).filter(k => s.picks[k]);
    s.picks = {...Object.fromEntries(keys.map(k => [k, s.picks[k]])), ...extra};
  }

  function openSession(id) {
    if (id === currentId) return;
    if (job) cancelJob();  // G-3: 다른 세션으로 가면 취소. "중단됨"은 runTask가 원래 세션에 남긴다
    currentId = id;
    persist();
    render();
  }

  function removeSession(s) {
    if (!confirm(`"${s.title}" 세션을 삭제할까요?`)) return;
    if (job && job.sid === s.id) cancelJob(true);  // G-7: 취소하고 결과는 버린다
    sessions = sessions.filter(x => x.id !== s.id);
    if (currentId === s.id) currentId = (sessions[0] || newSession()).id;
    persist();
    render();
  }

  function renameSession(s) {
    const t = prompt("세션 이름", s.title);
    if (t && t.trim()) updateSession(s.id, x => { x.title = t.trim(); });
  }

  function setStage(stage) { if (!isBusy()) updateSession(currentId, x => { x.stage = stage; }); }

  function getRun(id) {
    if (!id) return undefined;
    if (!(id in runCache)) {
      runCache[id] = "loading";
      fetch(`/api/runs/${encodeURIComponent(id)}`)
        .then(async res => { const d = await res.json(); runCache[id] = res.ok ? d : {error: d.error?.message || `HTTP ${res.status}`}; })
        .catch(e => { runCache[id] = {error: String(e)}; })
        .finally(render);
      return undefined;
    }
    const r = runCache[id];
    return r === "loading" || r.error ? undefined : r;
  }

  function loading(id) {
    const r = runCache[id];
    if (r && r.error) return h("p", {class: "err"}, `결과를 불러오지 못했습니다: ${r.error} `,
                               h("button", {onclick: () => { delete runCache[id]; render(); }}, "다시 불러오기"));
    return h("p", {class: "caption"}, "결과를 불러오는 중...");
  }

  // ---------- 작업 (SSE) ----------
  const isBusy = () => !!job && job.sid === currentId;

  function cancelJob(discard = false) {
    if (!job) return;
    job.discard = discard;
    job.ctl.abort();
    job = null;
  }

  async function readSSE(res, onEvent) {
    const reader = res.body.getReader(), dec = new TextDecoder();
    let buf = "";
    for (;;) {
      const {value, done} = await reader.read();
      if (done) return;
      buf += dec.decode(value, {stream: true});
      let i;
      while ((i = buf.indexOf("\n\n")) >= 0) {
        const block = buf.slice(0, i);
        buf = buf.slice(i + 2);
        let event = "message", data = "";
        for (const line of block.split("\n")) {
          if (line.startsWith("event:")) event = line.slice(6).trim();
          else if (line.startsWith("data:")) data += line.slice(5).trim();
        }
        if (data) onEvent(event, JSON.parse(data));
      }
    }
  }

  function failure(j, event, data) {
    const last = j.lines.length ? ` (단계: ${j.lines[j.lines.length - 1]})` : "";
    const note = event === "cancelled" ? `${j.label} 중단됨`
      : event === "budget" ? `${j.label} 중단: 호출 예산 초과 (${data.scope} ${data.used}/${data.limit})`
      : `${j.label} 실패${last}: ${data.error?.message || "알 수 없는 오류"}`;
    return msg("assistant", j.partial, {status: event === "cancelled" ? "cancelled" : "error", job: j.kind, note});
  }

  async function runTask({kind, label, url, body = {}, stream = false, replace = null, onResult = () => null}) {
    const s = getSession();
    if (!s || job) return;
    const j = job = {sid: s.id, kind, label, stream, replace, ctl: new AbortController(), partial: "", lines: [],
                     activity: `${label} 시작`};
    render();
    let end = null;
    try {
      const res = await fetch(url, {method: "POST", headers: {"Content-Type": "application/json"},
                                    body: JSON.stringify({...body, session_id: j.sid}), signal: j.ctl.signal});
      if (!res.ok) {
        const e = await res.json().catch(() => ({}));
        end = ["error", {error: e.error || {code: "http_error", message: `HTTP ${res.status}`, retryable: true}}];
      } else {
        await readSSE(res, (event, data) => {
          if (data.session_id !== j.sid) return;  // G-1
          if (event === "progress") {
            j.activity = data.message + (data.total ? ` (${data.current}/${data.total})` : "");
            j.lines.push(j.activity);
          } else if (event === "delta") j.partial += data.text;
          else end = [event, data];
          showJob(j);
        });
      }
    } catch (e) {
      end = e.name === "AbortError" ? ["cancelled", {}]
        : ["error", {error: {code: "network_error", message: String(e.message || e), retryable: true}}];
    }
    if (job === j) job = null;
    if (j.discard) return render(), "cancelled";
    end ||= ["error", {error: {code: "upstream_failed", message: "응답이 중간에 끊겼습니다.", retryable: true}}];
    const [event, data] = end;
    if (event === "error" && data.error?.code === "validation_error") {  // 입력 오류는 대화에 남기지 않고 폼에 표시
      formErrors[j.sid] = data.error.fields || [];
      render();
      return event;
    }
    updateSession(j.sid, x => place(x, event === "result" ? onResult(x, data) || null : failure(j, event, data), j.replace));
    return event;
  }

  function showJob(j) {  // 스트리밍 중에는 바뀐 곳만 고친다
    if (j.sid !== currentId) return;
    const lines = $("#job-lines"), text = $("#stream-text");
    if (lines) lines.replaceChildren(...j.lines.map(l => h("li", {}, l)));
    if (text) text.innerHTML = md(j.partial);
    $("#activity").textContent = j.activity;
  }

  function jobBox() {
    return h("div", {class: "jobbox"}, h("b", {}, `${job.label} 진행 중 `),
             h("button", {"data-keep": "1", onclick: () => cancelJob()}, "중단"),
             h("ul", {id: "job-lines"}, job.lines.map(l => h("li", {}, l))));
  }

  // ---------- 기본 작업: 추천, 후보 생성, 대화 ----------
  function startRecommend(s, {replace} = {}) {
    return runTask({kind: "recommend", label: "문제 추천", url: "/api/recommend", replace,
                    body: {profile: s.profile, rejections: s.rejections},
                    onResult: (x, d) => { x.picks = {recommend_run_id: d.run_id}; x.stage = "browse"; }});
  }

  function startResearch(s, {replace} = {}) {
    return runTask({kind: "research", label: "참고문헌 검색·후보 생성", url: "/api/research", replace,
                    body: {profile: s.profile, problem_id: s.picks.problem_id, messages: s.messages},
                    onResult: (x, d) => { keepPicks(x, "research_run_id", {research_run_id: d.run_id}); x.stage = "refine"; }});
  }

  function startChat(s, {question, replace} = {}) {
    let history = [...s.messages];
    if (replace) {  // 재시도: 실패한 답변 바로 앞의 질문을 다시 보낸다
      const i = s.messages.findIndex(m => m.id === replace);
      const q = s.messages.slice(0, i).findLastIndex(m => m.role === "user");
      if (q < 0) return;
      question = s.messages[q].content;
      history = s.messages.slice(0, q);
    } else {
      updateSession(s.id, x => {
        x.messages.push(msg("user", question));
        if (x.title === DEFAULT_TITLE) x.title = question.slice(0, 40);  // S-6
      });
    }
    return runTask({kind: "chat", label: "답변", url: "/api/chat", stream: true, replace,
                    body: {profile: s.profile, picks: s.picks, messages: history, message: question},
                    onResult: (x, d) => msg("assistant", d.content)});
  }

  function retry(s, m) { if (!isBusy() && jobs[m.job]) jobs[m.job](s, {replace: m.id}); }

  // ---------- 화면: ① 둘러보기 ----------
  function profileForm(s) {
    const d = drafts[s.id] ||= {...(s.profile || JSON.parse(sessionStorage.getItem(LAST) || "null") ||
                                    {lab_name: "", lab_topics: "", interests: ""})};
    const errs = formErrors[s.id] || [];
    return h("form", {onsubmit: e => { e.preventDefault(); submitProfile(s); }},
      FIELDS.map(([key, text, required, placeholder]) => h("label", {class: "field"},
        h("div", {}, text, required ? h("span", {class: "req"}, " *") : ""),
        h(key === "lab_name" ? "input" : "textarea", {name: key, placeholder, value: d[key] || "", rows: 2,
                                                      oninput: e => { d[key] = e.target.value; }}),
        errs.includes(key) ? h("div", {class: "err"}, "입력해 주세요") : "")),
      h("button", {type: "submit", class: "primary"}, "문제 추천 받기"));
  }

  function submitProfile(s) {
    const p = Object.fromEntries(FIELDS.map(([k]) => [k, (drafts[s.id][k] || "").trim()]));
    formErrors[s.id] = FIELDS.filter(([k, , required]) => required && !p[k]).map(([k]) => k);  // P-5
    if (formErrors[s.id].length) return render();
    sessionStorage.setItem(LAST, JSON.stringify(p));
    updateSession(s.id, x => { x.profile = p; });  // P-4: 세션은 시작 시점의 프로필을 복사해 둔다
    startRecommend(find(s.id));
  }

  function problemCard(s, c, n) {
    const rejected = s.rejections.find(r => r.target_id === c.problem_id);
    const picked = s.picks.problem_id === c.problem_id;
    const reason = h("input", {placeholder: "제외 이유 (다음 추천에 반영)"});
    return h("div", {class: "card" + (picked ? " picked" : "") + (rejected ? " rejected" : "")},
      h("h3", {}, `${n}. ${c.title}`, picked ? h("span", {class: "badge"}, "선택한 문제") : ""),
      h("p", {}, c.summary),
      c.reason ? h("p", {}, c.reason) : "",
      h("p", {class: "caption"}, "출처: ", h("a", {href: safeUrl(c.paper.url), target: "_blank", rel: "noopener"}, c.paper.title),
        ` (${c.paper.year}) · ${c.section_title} · p.${c.page}`),
      h("details", {}, h("summary", {}, "원문 근거"), h("blockquote", {}, c.quote)),
      rejected
        ? h("p", {class: "caption"}, `제외함${rejected.reason ? ": " + rejected.reason : ""} `,
            h("button", {onclick: () => updateSession(s.id, x => { x.rejections = x.rejections.filter(r => r.target_id !== c.problem_id); })}, "제외 취소"))
        : h("div", {class: "row"},
            h("button", {class: "primary", onclick: () => pickProblem(s, c)}, "이 문제 파고들기"),
            h("details", {class: "inline"}, h("summary", {}, "제외"), reason,
              h("button", {onclick: () => rejectProblem(s, c, reason.value)}, "제외하기"))),
      extras("problem", s, c));
  }

  function pickProblem(s, c) {
    updateSession(s.id, x => {
      if (x.picks.problem_id !== c.problem_id) keepPicks(x, "problem_id", {problem_id: c.problem_id});  // 같은 문제면 후보 재사용 (F-3)
      x.stage = "explore";
      if (x.title === DEFAULT_TITLE) x.title = c.title;  // S-6
    });
  }

  function rejectProblem(s, c, reason) {  // C-6
    updateSession(s.id, x => {
      x.rejections = x.rejections.filter(r => r.target_id !== c.problem_id)
        .concat({target: "problem", target_id: c.problem_id, reason: reason.trim()});
    });
  }

  function renderBrowse(el, s) {
    const id = s.picks.recommend_run_id, run = getRun(id);
    if (!run) return el.append(loading(id), h("button", {onclick: () => startRecommend(s)}, "다시 추천 받기"));
    if (run.intro) el.append(mdBlock(run.intro));
    run.recommendations.forEach((c, i) => el.append(problemCard(s, c, i + 1)));
    if (run.note) el.append(h("p", {class: "info"}, run.note));
    el.append(h("button", {onclick: () => startRecommend(s)},
                s.rejections.length ? "제외한 문제를 반영해 다시 추천 받기" : "다시 추천 받기"));
  }

  // ---------- 화면: ② 문제 파고들기, ③ 주제 다듬기 ----------
  const problemOf = s => getRun(s.picks.recommend_run_id)?.recommendations.find(c => c.problem_id === s.picks.problem_id);

  function renderExplore(el, s) {
    const c = problemOf(s);
    el.append(h("h2", {}, c ? c.title : s.picks.problem_id),
      c ? h("details", {}, h("summary", {}, "문제 내용과 원문 근거"), h("p", {}, c.summary), h("blockquote", {}, c.quote),
            h("p", {class: "caption"}, `출처: ${c.paper.title} · p.${c.page}`)) : "",
      h("div", {class: "row"},
        h("button", {onclick: () => setStage("browse")}, "다른 문제 둘러보기"),
        h("button", {class: "primary", onclick: () => startResearch(s)},
          s.picks.research_run_id ? "현재 대화를 반영해 다시 검색·생성" : "참고문헌 검색하고 후보 생성")),
      h("p", {class: "caption"}, "대화로 조건을 더한 뒤 생성 버튼을 누르면 프로필·대화·선택한 문제를 바탕으로 참고문헌을 검색합니다."));
    if (s.stage === "explore" && s.picks.research_run_id)
      el.append(h("p", {class: "caption"}, "이전에 만든 후보가 있습니다. 오른쪽 ③ 주제 다듬기를 눌러 다시 볼 수 있습니다."));
  }

  function topicCard(s, t) {
    const picked = s.picks.topic_id === t.id;
    return h("div", {class: "card" + (picked ? " picked" : "")},
      h("h4", {}, t.title, picked ? h("span", {class: "badge"}, "선택한 후보") : ""),
      t.summary ? h("p", {}, t.summary) : "",
      h("details", {}, h("summary", {}, "자세히"),
        ["gap", "approach", "evaluation"].filter(k => t[k]).map(k => h("p", {}, h("b", {}, `${k}: `), t[k])),
        t.support ? h("p", {class: "caption"}, `근거 참고문헌: ${t.support.join(", ")}`) : ""),
      picked ? "" : h("button", {onclick: () => pickTopic(s, t)}, "이 후보 선택"),
      extras("topic", s, t));
  }

  function pickTopic(s, t) {
    updateSession(s.id, x => {
      keepPicks(x, "topic_id", {topic_id: t.id});
      x.messages.push(msg("user", `이 후보를 다듬겠습니다: ${t.title}`));
    });
  }

  function renderRefine(el, s) {
    renderExplore(el, s);
    const id = s.picks.research_run_id, run = getRun(id);
    if (!run) return el.append(loading(id));
    el.append(h("div", {class: "msg assistant"}, h("div", {class: "who"}, "후보 설명 (중복 판정은 ④ 검증에서)"), mdBlock(run.presentation)),
      h("details", {}, h("summary", {}, `검색 참고문헌 ${run.references.length}편 · 검색 과정`),
        run.trace ? h("pre", {}, JSON.stringify(run.trace, null, 1)) : "",
        h("ul", {}, run.references.map(r => h("li", {}, `[${r.id}] `,
          safeUrl(r.url) ? h("a", {href: safeUrl(r.url), target: "_blank", rel: "noopener"}, r.title) : r.title, ` (${r.year ?? "연도 없음"})`)))),
      h("h3", {}, "다듬을 후보 선택"),
      ...run.topics.map(t => topicCard(s, t)));
  }

  // ---------- 화면 그리기 ----------
  function renderSessions() {
    $("#session-list").replaceChildren(...sessions.map(s => h("li", {class: s.id === currentId ? "current" : ""},
      h("a", {href: "#", title: s.title, onclick: e => { e.preventDefault(); openSession(s.id); }},
        s.title + (job && job.sid === s.id ? " (진행 중)" : "")),
      h("button", {title: "이름 바꾸기", onclick: () => renameSession(s)}, "이름"),
      h("button", {title: "삭제", onclick: () => removeSession(s)}, "삭제"))));
  }

  function renderStates(s) {
    $("#stages").replaceChildren(...STAGES.map(([key, text]) => {
      const p = panels[key];
      if (!p) return h("li", {}, `○ ${text} · 준비 중`);
      const current = s.stage === key, reachable = !p.reachable || p.reachable(s);
      const summary = reachable && p.summary ? p.summary(s) : "";
      const label = current ? `▶ ${text}` : reachable && !isBusy()
        ? h("a", {href: "#", onclick: e => { e.preventDefault(); setStage(key); }}, text) : text;
      return h("li", {class: (current ? "current " : "") + (reachable ? "reachable" : "")}, label,
               summary ? h("div", {class: "caption"}, summary) : "");
    }));
    $("#activity").textContent = isBusy() ? job.activity : "";
  }

  function renderStage(s) {
    const el = $("#stage-view");
    el.replaceChildren();
    if (!s.picks.recommend_run_id) el.append(profileForm(s));
    else {
      const p = s.profile || {};
      el.append(h("p", {class: "caption"}, [p.lab_name, p.lab_topics, p.interests].filter(Boolean).join(" · ")));
      const panel = panels[s.stage];
      if (panel) panel.render(el, s);
      else el.append(h("p", {class: "info"}, `${(STAGES.find(x => x[0] === s.stage) || [, s.stage])[1]}: 준비 중입니다.`));
      const buttons = (actions[s.stage] || []).map(a => h("button", {onclick: () => a.run(s), disabled: a.enabled && !a.enabled(s)}, a.label));
      if (buttons.length) el.append(h("div", {class: "row"}, buttons));
    }
    if (isBusy() && !job.stream) el.append(jobBox());
  }

  function renderMessages(s) {
    const el = $("#messages");
    el.replaceChildren(...s.messages.map(m => {
      const failed = m.status === "cancelled" || m.status === "error";
      return h("div", {class: `msg ${m.role} ${m.status}`},
        h("div", {class: "who"}, m.role === "user" ? "나" : "도우미"),
        m.content ? (m.role === "user" ? h("div", {class: "text"}, m.content) : mdBlock(m.content)) : "",
        failed ? h("div", {class: "status"}, m.note || (m.status === "cancelled" ? "중단됨" : "실패"), " ",
                   jobs[m.job] ? h("button", {onclick: () => retry(s, m)}, "재시도") : "") : "");
    }));
    if (isBusy() && job.stream)
      el.append(h("div", {class: "msg assistant"}, h("div", {class: "who"}, "도우미 (작성 중) ",
                  h("button", {"data-keep": "1", onclick: () => cancelJob()}, "중단")), mdBlock(job.partial, "stream-text")));
  }

  function render() {
    let s = getSession();
    if (!s) { s = sessions[0] || newSession(); currentId = s.id; persist(); }
    renderSessions();
    renderStates(s);
    renderStage(s);
    renderMessages(s);
    $("#chat-form").hidden = !s.picks.recommend_run_id;
    $("#chat-input").disabled = $("#chat-form button").disabled = isBusy();  // 채팅 입력란은 다시 그리지 않으므로 직접 푼다
    if (isBusy())  // G-2: 작업 중에는 그 세션의 입력을 잠근다
      document.querySelectorAll("main button, main input, main textarea, main select")
        .forEach(x => { if (!x.dataset.keep) x.disabled = true; });
  }

  // ---------- 확장 훅 ----------
  function registerPanel(stage, panel) { panels[stage] = panel; }
  function addAction(stage, action) { (actions[stage] ||= []).push(action); }
  function registerJob(kind, start) { jobs[kind] = start; }
  const cardExtras = [];
  function addCardExtra(fn) { cardExtras.push(fn); }
  function extras(kind, s, item) { return cardExtras.map(fn => fn(kind, s, item)); }

  registerPanel("browse", {render: renderBrowse, reachable: s => !!s.picks.recommend_run_id, summary: s => {
    const run = getRun(s.picks.recommend_run_id);
    if (!run) return "";
    return (run.recommendations.length ? `추천 ${run.recommendations.length}개` : "관련 문제 없음") +
           (s.rejections.length ? ` · 제외 ${s.rejections.length}개` : "");
  }});
  registerPanel("explore", {render: renderExplore, reachable: s => !!s.picks.problem_id,
                            summary: s => problemOf(s)?.title || ""});
  registerPanel("refine", {render: renderRefine, reachable: s => !!s.picks.research_run_id, summary: s => {
    const run = getRun(s.picks.research_run_id);
    const topic = run?.topics.find(t => t.id === s.picks.topic_id);
    return run ? `후보 ${run.topics.length}개` + (topic ? ` · 선택: ${topic.title}` : "") : "";
  }});
  registerJob("recommend", startRecommend);
  registerJob("research", startResearch);
  registerJob("chat", startChat);

  window.App = {STAGES, registerPanel, addAction, registerJob, addCardExtra, runTask, getSession, updateSession, setStage,
                getRun, loading, isBusy, render, h, md, msg};

  document.addEventListener("DOMContentLoaded", () => {
    $("#new-session").onclick = () => openSession(newSession().id);
    const input = $("#chat-input");
    $("#chat-form").onsubmit = e => {
      e.preventDefault();
      const s = getSession(), text = input.value.trim();
      if (!s || !text || isBusy()) return;
      input.value = "";
      startChat(s, {question: text});
    };
    input.onkeydown = e => {
      if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); $("#chat-form").requestSubmit(); }
    };
    addEventListener("beforeunload", () => {  // 새로고침·탭 닫기도 취소로 남긴다 (G-4)
      if (!job) return;
      const j = job;
      cancelJob(true);
      const s = find(j.sid);
      if (s) { place(s, failure(j, "cancelled", {}), j.replace); persist(); }
    });
    render();
  });
})();
