/* 연구 주제 탐색 화면. 세션은 API 명세 2.2 형식이다.
   - 비로그인: sessionStorage에 저장한다. 탭을 닫으면 사라진다 (S-3).
   - 로그인 (static/auth.js): 바뀐 세션을 PUT /api/sessions/{id}로 서버(Supabase)에 저장한다 (S-1, S-2).
     목록은 요약만 받고 세션을 열 때 전체를 받는다. 다른 탭이 먼저 저장했으면(409) 서버 것으로 바꾼다.
     로그인하면 이 탭에서 비로그인으로 만든 세션을 계정으로 옮길지 묻는다 (A-5). 프로필 기본값은 계정에 둔다 (P-3).
   메시지에는 재시도를 위해 job(작업 종류)과 note(중단·실패 안내)가 더 붙을 수 있다.

   화면은 하나의 대화다 (이슈 #15). 추천·후보·검증·확정 결과도 대화 메시지로 아래에 쌓인다.
   - 결과 메시지는 kind(problems, problem, topics, verify, final)와 run_id를 가진다. content는 LLM에 넘길 짧은 요약이다.
   - 버튼이 살아 있는 카드는 맨 아래 결과 메시지 하나뿐이다. 지나간 카드는 고른 것(picked)만 표시한다.
     앞 단계로 되돌아가는 기능은 없다. 방향을 바꾸면 새 메시지가 아래에 붙는다.
   - 오른쪽 패널은 현재 단계를 보여 주기만 한다. 눌러서 이동하지 않는다.
   - stage와 picks는 지금 위치와 마지막 선택이다. 대화 API와 다음 작업 요청에 쓴다.

   확장 모듈 (static/modules/<기능>.js, app.js 다음에 실행되고 첫 화면은 DOMContentLoaded 때 그린다):
   App.registerCard(kind, render(el, s, m, active))   kind 메시지의 본문을 그린다. active면 다음 단계 버튼을 단다.
   App.registerStage(stage, summary(s))               오른쪽 패널에서 그 단계 아래에 보일 한 줄.
   App.registerJob(kind, start(s, {replace}))          "재시도"와 App.startJob이 부를 시작 함수.
   App.startJob(kind, s, opts)                        등록한 작업을 시작한다 (예: 검증 카드에서 finalize). opts는 시작 함수로 넘긴다.
   App.registerActions(kind, fn(s, m) -> [{name, target, label}])  kind 카드가 맨 아래일 때 채팅으로 할 수 있는 행동 (이슈 #23).
   App.registerAction(name, fn(s, action))           채팅이 그 행동을 요청했을 때 부를 함수. 사용자 말은 이미 대화에 있다.
   App.currentRevision(s, research_run_id, topic_id) 적용한 후보 수정안 {run_id, version} 또는 null (이슈 #12).
   App.pickTopic(s, m, t, n, mode, {echo})          ③ 후보를 고른다. mode: "verify" | "chat" | "select".
   App.addCardExtra(fn(kind, s, item, m) -> Node|null) 카드 끝에 붙일 요소 (예: 피드백 버튼).
       kind: "problem"(① 추천 카드, item = 추천 항목) · "topic"(③ 후보 카드, item = 후보). m은 카드가 든 메시지.
   App.runTask({kind, label, url, body, stream, replace, onResult(s, data)}) -> Promise<"result"|"cancelled"|"error"|"budget">
       SSE 작업 하나를 돌린다. body에 session_id를 붙이고, 끝날 때까지 그 세션의 입력을 잠근다 (G-2).
       다른 세션으로 가면 abort하고 시작한 세션에 "중단됨" 메시지(받은 부분 응답 포함)를 남긴다 (G-1, G-3, G-4).
       onResult는 끝까지 성공했을 때만 시작한 세션 객체로 불린다. stage 변경은 여기서 한다 (G-6).
       onResult가 {role, content, kind, run_id} 메시지를 돌려주면 대화에 넣는다. replace는 재시도로 대신할 메시지 id.
   App.getSession() / App.updateSession(id, fn) / App.msg(role, content, extra)
   App.getRun(run_id)   /api/runs 결과 캐시. 처음에는 undefined를 돌려주고, 불러오면 다시 그린다.
   App.loading(run_id)  getRun이 undefined일 때 보여 줄 요소. 불러오는 중이면 안내, 실패했으면 오류와 "다시 불러오기".
   App.isBusy() / App.render() / App.h(tag, attrs, ...children) / App.md(text) / App.mdBlock(text)
   App.btn(label, onclick, cls)  버튼. cls는 "primary"(검은 버튼) · ""(테두리 버튼) · "ghost"(글자 버튼)
*/
(() => {
  const STAGES = [["browse", "① 둘러보기"], ["explore", "② 문제 파고들기"], ["refine", "③ 주제 다듬기"],
                  ["verify", "④ 검증"], ["final", "⑤ 확정"]];
  const FIELDS = [["lab_name", "연구실 정보", true, "예: 보안 신뢰 컴퓨팅 연구실"],
                  ["lab_topics", "연구실 연구 주제", true, "예: AI 보안, 네트워크·시스템 보안, 트래픽 분석"],
                  ["interests", "사용자 관심사", false, "예: LLM 에이전트 프롬프트 인젝션 방어 (선택)"]];
  const PICK_ORDER = ["recommend_run_id", "problem_id", "research_run_id", "topic_id", "revision_run_id"];
  const DEFAULT_TITLE = "새 대화";
  const KEY = "rta.sessions", CUR = "rta.current", LAST = "rta.last_profile", SKIP_IMPORT = "rta.skip_import";
  const JSON_HDR = {"Content-Type": "application/json"};

  let sessions = [];  // 로그인 때는 아직 전체를 받지 않은 세션이 {id, title, stage, updated_at, stub: true}로 들어 있다
  let currentId = sessionStorage.getItem(CUR);
  let account = null, accountProfile = null, notice = "";
  const versions = {}, chains = {}, pendingSave = new Set(), fullLoading = new Set(), fullErrors = {};
  let job = null;  // 도는 작업은 늘 화면의 세션 것 하나뿐이다 (세션을 옮기면 취소되므로)
  const runCache = {}, cards = {}, stageSummary = {}, jobs = {}, drafts = {}, formErrors = {};
  const actionLists = {}, actionHandlers = {};
  let scrollKey = "", scrollTarget = null, stickBottom = false;

  // ---------- 도구 ----------
  const $ = sel => document.querySelector(sel);
  const now = () => new Date().toISOString();
  const rid = p => p + Array.from(crypto.getRandomValues(new Uint8Array(8)), b => b.toString(16).padStart(2, "0")).join("");
  const safeUrl = u => /^https?:\/\//.test(u || "") ? u : null;
  // 화면용 한국어 (이슈 #39). 내부 데이터는 영어이고, 한국어가 없는 예전 결과는 영어를 그대로 보인다
  const ko = (x, k) => (x.ko && x.ko[k]) || x[k];
  const TOPIC_LABEL = {gap: "연구 공백 (gap)", approach: "방법 (approach)", evaluation: "평가 (evaluation)"};
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
  const btn = (label, onclick, cls = "", extra = {}) => h("button", {type: "button", class: `btn ${cls}`.trim(), onclick, ...extra}, label);

  // ---------- 세션 저장: 비로그인은 sessionStorage, 로그인은 서버 ----------
  function persist() {
    if (!account) sessionStorage.setItem(KEY, JSON.stringify(sessions));
    sessionStorage.setItem(CUR, currentId || "");
  }

  async function getJSON(url) {
    const res = await fetch(url);
    if (!res.ok) throw new Error((await res.json().catch(() => ({}))).error?.message || `HTTP ${res.status}`);
    return res.json();
  }

  function setNotice(text) { notice = text; renderAccount(); }

  function enqueue(id, fn) {  // 한 세션의 저장·삭제는 차례대로 보낸다
    chains[id] = (chains[id] || Promise.resolve()).then(fn).catch(e => setNotice(`저장 실패: ${e.message || e}`));
  }

  function queueSave(id) {  // 이미 기다리는 저장이 있으면 그것이 최신 내용을 보낸다
    if (pendingSave.has(id)) return;
    pendingSave.add(id);
    enqueue(id, async () => { pendingSave.delete(id); await saveOnce(id); });
  }

  function replaceSession(s, data) { for (const k of Object.keys(s)) delete s[k]; Object.assign(s, data); }

  async function saveOnce(id) {
    const s = find(id);
    if (!account || !s || s.stub) return;
    const res = await fetch(`/api/sessions/${encodeURIComponent(id)}`, {method: "PUT", headers: JSON_HDR,
      body: JSON.stringify({session: s, base_updated_at: versions[id] ?? null})});
    const d = await res.json().catch(() => ({}));
    if (res.ok) {
      versions[id] = d.updated_at;
      if (notice.startsWith("저장 실패")) setNotice("");
      return;
    }
    const current = d.error?.code === "version_conflict" && d.error.current;
    if (!current) throw new Error(d.error?.message || `HTTP ${res.status}`);
    if (job && job.sid === id) cancelJob(true);  // 다른 탭이 먼저 저장했다. 서버 것을 따른다
    replaceSession(s, current);
    versions[id] = current.updated_at;
    notice = "다른 탭에서 먼저 바뀐 세션이라 최신 내용을 다시 불러왔습니다.";
    render();
  }

  function deleteOnServer(id) {
    enqueue(id, async () => {
      if (versions[id] === undefined) return;  // 서버에 저장된 적 없는 세션
      const res = await fetch(`/api/sessions/${encodeURIComponent(id)}`, {method: "DELETE"});
      if (!res.ok && res.status !== 404) throw new Error(`HTTP ${res.status}`);
      delete versions[id];
    });
  }

  function loadFull(id) {  // 목록에서 고른 세션의 전체 내용을 받는다
    if (fullLoading.has(id) || fullErrors[id]) return;
    fullLoading.add(id);
    fetch(`/api/sessions/${encodeURIComponent(id)}`).then(async res => {
      const s = find(id), d = await res.json().catch(() => ({}));
      if (res.status === 404) sessions = sessions.filter(x => x.id !== id);
      else if (!res.ok) fullErrors[id] = d.error?.message || `HTTP ${res.status}`;
      else if (s && s.stub) { replaceSession(s, d); versions[id] = d.updated_at; }
    }).catch(e => { fullErrors[id] = String(e.message || e); })
      .finally(() => { fullLoading.delete(id); render(); });
  }

  async function offerImport() {  // A-5: 비로그인 때 이 탭에서 만든 세션을 계정으로 옮긴다
    const all = JSON.parse(sessionStorage.getItem(KEY) || "[]");
    const local = all.filter(s => s.messages?.length || s.picks?.recommend_run_id);
    if (!local.length || sessionStorage.getItem(SKIP_IMPORT) === account.id) return;
    if (!confirm(`로그인 전에 이 탭에서 만든 대화 ${local.length}개를 계정에 저장할까요?\n` +
                 "취소하면 계정에 저장하지 않고, 로그아웃하면 이 탭에서 다시 볼 수 있습니다.")) {
      sessionStorage.setItem(SKIP_IMPORT, account.id);
      return;
    }
    const res = await fetch("/api/sessions/import", {method: "POST", headers: JSON_HDR, body: JSON.stringify({sessions: local})});
    if (!res.ok) return setNotice("대화를 계정으로 옮기지 못했습니다.");
    const moved = Object.fromEntries((await res.json()).imported.map(x => [x.old_id, x.new_id]));
    if (moved[currentId]) currentId = moved[currentId];
    sessionStorage.setItem(KEY, JSON.stringify(all.filter(s => !(s.id in moved))));
  }

  async function loadSessions() {
    for (const k of Object.keys(versions)) delete versions[k];
    for (const k of Object.keys(fullErrors)) delete fullErrors[k];
    accountProfile = null;
    if (!account) { sessions = JSON.parse(sessionStorage.getItem(KEY) || "[]"); return; }
    try {
      await offerImport();
      const [list, profile] = await Promise.all([getJSON("/api/sessions"), getJSON("/api/profile")]);
      sessions = list.map(x => ({...x, stub: true}));
      list.forEach(x => { versions[x.id] = x.updated_at; });
      accountProfile = profile;
    } catch (e) {
      sessions = [];
      notice = `저장된 대화를 불러오지 못했습니다: ${e.message || e}`;
    }
  }

  async function switchAccount(user) {  // 로그인·로그아웃. 도는 작업은 취소하고 결과를 버린다 (G-7)
    if (job) cancelJob(true);
    account = user;
    notice = "";
    for (const k of Object.keys(drafts)) delete drafts[k];
    await loadSessions();
    render();
  }

  async function deleteAccount() {
    if (!confirm("계정과 저장된 모든 대화를 삭제할까요? 되돌릴 수 없습니다.")) return;
    const res = await fetch("/api/me", {method: "DELETE"});
    if (!res.ok) return setNotice("계정을 삭제하지 못했습니다.");
    await window.Auth.signOut().catch(() => null);
  }
  const find = id => sessions.find(s => s.id === id);
  const getSession = () => find(currentId);

  function updateSession(id, fn) {  // 결과는 항상 id로 찾은 세션에만 쓴다 (G-1). 지워진 세션이면 버린다 (G-7)
    const s = find(id);
    if (!s) return;
    if (s.stub) return;
    fn(s);
    s.updated_at = now();
    persist();
    if (account) queueSave(id);
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
    if (account) deleteOnServer(s.id);
    if (currentId === s.id) currentId = (sessions[0] || newSession()).id;
    persist();
    render();
  }

  function renameSession(s) {
    const t = prompt("세션 이름", s.title);
    if (t && t.trim()) updateSession(s.id, x => { x.title = t.trim(); });
  }

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
                               btn("다시 불러오기", () => { delete runCache[id]; render(); }, "ghost"),
                               h("span", {class: "caption"}, " 결과는 24시간 보관됩니다."));
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
    const sc = $("#scroll"), near = sc.scrollHeight - sc.scrollTop - sc.clientHeight < 120;
    const lines = $("#job-lines"), text = $("#stream-text"), now = $("#job-activity");
    if (lines) lines.replaceChildren(...j.lines.map(l => h("li", {}, l)));
    if (text && j.partial) { text.className = "md"; text.innerHTML = md(j.partial); }
    if (now) now.textContent = j.activity;
    $("#activity").textContent = j.activity;
    if (near) sc.scrollTop = sc.scrollHeight;  // 사용자가 위로 올려 읽는 중이면 끌어내리지 않는다
  }

  const avatar = () => h("div", {class: "avatar", "aria-hidden": "true"}, "✦");

  function jobBubble() {  // 도는 작업은 대화 맨 아래에 도우미 메시지로 보인다
    if (job.stream)
      return h("div", {class: "msg assistant"}, avatar(), h("div", {class: "body"},
        job.partial ? mdBlock(job.partial, "stream-text")
                    : h("div", {id: "stream-text", class: "typing"}, h("i"), h("i"), h("i"))));
    return h("div", {class: "msg assistant"}, avatar(), h("div", {class: "body"},
      h("div", {class: "jobbox"},
        h("div", {class: "job-head"}, h("span", {class: "spinner"}), h("b", {}, job.label),
          btn("중단", () => cancelJob(), "ghost", {"data-keep": "1"})),
        h("div", {id: "job-activity", class: "caption"}, job.activity),
        h("details", {}, h("summary", {}, "진행 기록"), h("ol", {id: "job-lines"}, job.lines.map(l => h("li", {}, l)))))));
  }

  // ---------- 대화 메시지 도구 ----------
  const say = (x, role, content, extra) => { x.messages.push(msg(role, content, extra)); };
  function mark(x, m, key, value) { const y = x.messages.find(z => z.id === m.id); if (y) y[key] = value; }
  const cardIndex = s => s.messages.findLastIndex(m => m.kind && cards[m.kind] && m.status === "done");
  const profileText = p => [`연구실: ${p.lab_name}`, `연구 주제: ${p.lab_topics}`, p.interests ? `관심사: ${p.interests}` : ""]
    .filter(Boolean).join("\n");

  function migrate(s) {  // 대화형으로 바꾸기 전에 만든 세션: picks로 결과 메시지를 만들어 대화 앞에 넣는다
    if (s.stub || s.v === 2) return;
    s.v = 2;
    if (s.messages.some(m => m.kind)) return;
    const p = s.picks || {}, front = [];
    if (s.profile) front.push(msg("user", profileText(s.profile), {kind: "profile"}));
    if (p.recommend_run_id) front.push(msg("assistant", "추천한 Open Problem", {kind: "problems", run_id: p.recommend_run_id, picked: p.problem_id}));
    if (p.problem_id) front.push(msg("assistant", "선택한 문제", {kind: "problem", run_id: p.recommend_run_id, problem_id: p.problem_id}));
    if (p.research_run_id) front.push(msg("assistant", "연구 주제 후보", {kind: "topics", run_id: p.research_run_id, picked: p.topic_id}));
    if (p.verify_run_id) front.push(msg("assistant", "검증 결과", {kind: "verify", run_id: p.verify_run_id,
                                                                  research_run_id: p.research_run_id, topic_id: p.topic_id}));
    if (p.final_run_id) front.push(msg("assistant", "최종 주제 카드", {kind: "final", run_id: p.final_run_id}));
    s.messages.unshift(...front);
  }

  // ---------- 기본 작업: 추천, 후보 생성, 대화 ----------
  function shownProblems(s) {  // 이 세션에서 이미 보여 준 problem_id. 다시 추천할 때 빼도록 보낸다 (이슈 #21)
    const ids = new Set();
    for (const m of s.messages) {
      if (m.kind !== "problems") continue;
      const r = runCache[m.run_id];  // problem_ids가 없는 예전 메시지는 불러온 결과에서 꺼낸다
      const list = m.problem_ids || (r && r.recommendations ? r.recommendations.map(c => c.problem_id) : []);
      list.forEach(id => ids.add(id));
    }
    return [...ids];
  }

  function problemOffset(s, m) {  // m 앞의 추천 카드에서 보여 준 문제 수. 다시 추천한 문제는 번호를 이어 매긴다 (이슈 #23)
    const i = s.messages.findIndex(x => x.id === m.id);
    return shownProblems({messages: i < 0 ? s.messages : s.messages.slice(0, i)}).length;
  }

  function startRecommend(s, {replace} = {}) {
    return runTask({kind: "recommend", label: "문제 추천", url: "/api/recommend", replace,
                    body: {profile: s.profile, rejections: s.rejections, exclude: shownProblems(s)},
                    onResult: (x, d) => {
                      const first = d.first_number || shownProblems(x).length + 1;
                      x.picks = {recommend_run_id: d.run_id};
                      x.stage = "browse";
                      const recs = d.recommendations || [];
                      const list = recs.map((c, i) => `${first + i}. ${c.title}`).join("\n");
                      return msg("assistant", list ? `추천한 Open Problem:\n${list}` : "관련 문제를 찾지 못했습니다.",
                                 {kind: "problems", run_id: d.run_id, problem_ids: recs.map(c => c.problem_id)});
                    }});
  }

  function startResearch(s, {replace} = {}) {
    return runTask({kind: "research", label: "참고문헌 검색·후보 생성", url: "/api/research", replace,
                    body: {profile: s.profile, problem_id: s.picks.problem_id, messages: s.messages, memo: s.memo || null},
                    onResult: (x, d) => {
                      keepPicks(x, "research_run_id", {research_run_id: d.run_id});
                      x.stage = "refine";
                      return msg("assistant", d.presentation || "연구 주제 후보를 만들었습니다.", {kind: "topics", run_id: d.run_id});
                    }});
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
                    body: {profile: s.profile, picks: s.picks, messages: history, message: question, actions: chatActions(s), stage: s.stage,
                           memo: s.memo || null},
                    onResult: (x, d) => {
                      if (d.compact) setTimeout(() => compactSession(x.id));  // 입력이 5만 토큰을 넘었다. 뒤에서 압축한다
                      if (!d.action) return msg("assistant", d.content);
                      setTimeout(() => runAction(x.id, d.action));  // 작업 잠금이 풀린 뒤 버튼과 같은 함수를 부른다
                      return null;
                    }});
  }

  // ---------- 대화 압축 (이슈 #31) ----------
  // 오래된 주변 대화를 서버가 항목 목록(memo)으로 줄여 준다. memo는 세션에 저장하고 다음 요청마다 보낸다.
  // 화면에는 보이지 않고 원래 메시지도 그대로 둔다. 입력 잠금 없이 뒤에서 돌며, 실패하면 다음 기회에 다시 한다.
  const compacting = new Set();
  async function compactSession(id) {
    const s = find(id);
    if (!s || s.stub || compacting.has(id)) return;
    compacting.add(id);
    try {
      const res = await fetch("/api/chat/compact", {method: "POST", headers: JSON_HDR,
        body: JSON.stringify({session_id: id, messages: s.messages, memo: s.memo || null})});
      if (!res.ok) return;
      let memo = null;
      await readSSE(res, (event, data) => { if (event === "result" && data.session_id === id) memo = data.memo; });
      if (memo) updateSession(id, x => { x.memo = memo; });
    } catch (e) {
      console.warn("대화 압축 실패", e);
    } finally {
      compacting.delete(id);
    }
  }

  // ---------- 채팅으로 다음 단계 진행 (이슈 #23) ----------
  // 지금 단계에서 버튼으로 할 수 있는 행동을 서버에 보내면, 서버가 채팅이 그중 하나를 요청하는지 판별해 돌려준다.
  function chatActions(s) {
    const last = s.messages[cardIndex(s)];
    if (!last) return [];
    if (actionLists[last.kind]) return actionLists[last.kind](s, last);
    if (last.kind === "problem") return [{name: "start_research", target: "", label: "고른 문제로 참고문헌을 검색하고 연구 주제 후보 만들기"}];
    if (last.kind !== "problems") return [];
    // 번호가 추천 카드 사이에서 겹치지 않으니 화면 번호를 target으로 쓴다. LLM이 긴 id를 베끼지 않아도 된다
    const acts = pickableProblems(s).map(({n, c}) => ({name: "pick_problem", target: String(n), label: `${n}번 문제로 진행: ${c.title}`}));
    const lastRun = getRun(last.run_id);
    if (lastRun && (lastRun.recommendations.length || !shownProblems(s).length))
      acts.push({name: "re_recommend", target: "", label: "앞에서 보여 준 문제를 빼고 Open Problem 다시 추천"});
    return acts;
  }

  function pickableProblems(s) {  // 문제를 고르기 전이면 앞선 추천 카드에서도 고를 수 있다. [{n, m, c}]
    const from = s.messages.findLastIndex(x => x.kind && x.kind !== "problems" && cards[x.kind]) + 1;
    const out = [];
    for (const m of s.messages.slice(from)) {
      const run = m.kind === "problems" && m.status === "done" && getRun(m.run_id);
      if (!run) continue;
      const off = problemOffset(s, m);
      run.recommendations.forEach((c, i) => out.push({n: off + i + 1, m, c}));
    }
    return out;
  }

  function runAction(sid, a) {
    const s = find(sid);
    if (!s || s.id !== currentId || isBusy()) return;
    if (a.name === "pick_problem") {
      const hit = pickableProblems(s).find(p => String(p.n) === a.target);
      if (hit) pickProblem(s, hit.m, hit.c, hit.n, {echo: false});
    } else if (a.name === "re_recommend") startRecommend(s);
    else if (a.name === "start_research") startResearch(s);
    else if (actionHandlers[a.name]) actionHandlers[a.name](s, a);
  }

  // ---------- ③·④ 후보 행동: 검증, 고치기, 확정 (이슈 #12) ----------
  function currentRevision(s, researchRunId, topicId) {  // 그 후보에 마지막으로 적용한 수정안
    const m = s.messages.findLast(x => x.kind === "revision" && x.applied && x.research_run_id === researchRunId
                                       && x.topic_id === topicId);
    return m ? {run_id: m.run_id, version: m.version} : null;
  }

  function topicsMessage(s) {  // 번호로 후보를 고를 때 쓰는 마지막 후보 카드
    return s.messages.findLast(x => x.kind === "topics" && x.status === "done");
  }

  function topicActions(s) {
    const m = topicsMessage(s), run = m && getRun(m.run_id);
    if (!run) return [];
    return run.topics.flatMap((t, i) => {
      const n = i + 1, mine = s.picks.research_run_id === m.run_id && s.picks.topic_id === t.id ? " (고른 후보)" : "";
      return [{name: "verify_topic", target: String(n), label: `${n}번 후보 검증${mine}: ${t.title}`},
              {name: "revise_topic", target: String(n), label: `${n}번 후보 고치기${mine}: 사용자가 말한 변경을 후보에 반영`}];
    });
  }

  function topicByNumber(s, n) {
    const m = topicsMessage(s), run = m && getRun(m.run_id), t = run && run.topics[Number(n) - 1];
    return t ? {m, t} : null;
  }

  registerActions("topics", topicActions);
  registerAction("verify_topic", (s, a) => {
    const hit = a.target && topicByNumber(s, a.target);
    if (hit) pickTopic(s, hit.m, hit.t, Number(a.target), "verify", {echo: false});
    else if (!a.target && s.picks.topic_id) startJob("verify", s);
  });
  registerAction("revise_topic", (s, a) => {
    const hit = a.target && topicByNumber(s, a.target);
    if (hit) pickTopic(s, hit.m, hit.t, Number(a.target), "select");
    if (hit || (!a.target && s.picks.topic_id)) startJob("revise", find(s.id));
  });
  registerAction("finalize", s => { if (s.picks.topic_id) startJob("finalize", s); });

  function retry(s, m) { if (!isBusy() && jobs[m.job]) jobs[m.job](s, {replace: m.id}); }

  // ---------- 시작: 프로필 ----------
  function profileForm(s) {
    const last = account ? accountProfile : JSON.parse(sessionStorage.getItem(LAST) || "null");
    const d = drafts[s.id] ||= {...(s.profile || last ||
                                    {lab_name: "", lab_topics: "", interests: ""})};
    const errs = formErrors[s.id] || [];
    return h("form", {class: "profile-form", onsubmit: e => { e.preventDefault(); submitProfile(s); }},
      FIELDS.map(([key, text, required, placeholder]) => h("label", {class: "field"},
        h("div", {class: "field-label"}, text, required ? h("span", {class: "req"}, " *") : ""),
        h(key === "lab_name" ? "input" : "textarea", {name: key, placeholder, value: d[key] || "", rows: 2,
                                                      oninput: e => { d[key] = e.target.value; }}),
        errs.includes(key) ? h("div", {class: "err"}, "입력해 주세요") : "")),
      h("div", {class: "row end"}, h("button", {type: "submit", class: "btn primary"}, "문제 추천 받기")));
  }

  function submitProfile(s) {
    const p = Object.fromEntries(FIELDS.map(([k]) => [k, (drafts[s.id][k] || "").trim()]));
    formErrors[s.id] = FIELDS.filter(([k, , required]) => required && !p[k]).map(([k]) => k);  // P-5
    if (formErrors[s.id].length) return render();
    if (account) {  // P-3: 다음 세션에서 다시 입력하지 않도록 계정에 둔다
      accountProfile = p;
      fetch("/api/profile", {method: "PUT", headers: JSON_HDR, body: JSON.stringify(p)}).catch(() => null);
    } else sessionStorage.setItem(LAST, JSON.stringify(p));
    updateSession(s.id, x => {  // P-4: 세션은 시작 시점의 프로필을 복사해 둔다
      x.profile = p;
      say(x, "user", profileText(p), {kind: "profile"});
    });
    startRecommend(find(s.id));
  }

  function welcome(s) {  // 저장하지 않는 첫 안내. 프로필을 넣기 전에는 가운데에 크게 보인다
    const text = "연구실 정보와 관심사를 알려 주시면 관련 AI 논문의 Open Problem을 추천합니다. " +
                 "고른 문제로 연구 주제 후보를 만들고, 기존 연구와 겹치는지 검증한 뒤 최종 주제를 정리해 드립니다.";
    if (!s.profile) return h("div", {class: "hero"}, h("h2", {}, "어떤 연구를 하고 계신가요?"), h("p", {class: "caption"}, text),
                             h("div", {class: "card form-card"}, profileForm(s)));
    return h("div", {class: "msg assistant"}, avatar(), h("div", {class: "body"}, h("p", {}, text)));
  }

  // ---------- ① 추천 메시지 ----------
  const sourceLine = c => h("p", {class: "caption"}, "출처: ",
    h("a", {href: safeUrl(c.paper.url), target: "_blank", rel: "noopener"}, c.paper.title),
    ` (${c.paper.year}) · ${c.section_title} · p.${c.page}`);

  function problemCard(s, m, c, n, active) {
    const picked = m.picked === c.problem_id;
    const foot = active ? h("div", {class: "row"}, btn("이 문제로 진행", () => pickProblem(s, m, c, n), "primary")) : "";
    return h("div", {class: "card" + (picked ? " picked" : "")},
      h("div", {class: "card-title"}, h("span", {class: "num"}, n), c.title, picked ? h("span", {class: "badge"}, "선택함") : ""),
      h("p", {}, c.summary_ko || c.summary),
      c.reason ? h("p", {class: "reason"}, c.reason) : "",
      sourceLine(c),
      h("details", {}, h("summary", {}, "원문 인용"), h("blockquote", {}, c.quote)),
      foot,
      extras("problem", s, c, m));
  }

  function pickProblem(s, m, c, n, {echo = true} = {}) {  // echo: 채팅으로 고르면 사용자 말이 이미 대화에 있다
    updateSession(s.id, x => {
      mark(x, m, "picked", c.problem_id);
      // 앞선 추천 카드에서 골라도 그 카드의 결과를 가리키게 한다 (대화 컨텍스트, ⑤의 추천 이유가 이 결과를 읽는다)
      keepPicks(x, "recommend_run_id", {recommend_run_id: m.run_id, problem_id: c.problem_id});
      x.stage = "explore";
      if (x.title === DEFAULT_TITLE) x.title = c.title;  // S-6
      if (echo) say(x, "user", `${n}번 문제로 진행할게요: ${c.title}`);
      say(x, "assistant", `선택한 문제: ${c.title}\n${c.summary_ko || c.summary}`, {kind: "problem", run_id: m.run_id, problem_id: c.problem_id});
    });
  }

  function renderProblems(el, s, m, active) {
    const run = getRun(m.run_id);
    if (!run) return el.append(loading(m.run_id));
    if (run.intro) el.append(mdBlock(run.intro));
    // 다시 추천을 받아도 문제를 고르기 전까지는 앞선 추천 카드에서도 고를 수 있다
    const i = s.messages.findIndex(x => x.id === m.id);
    const pickable = active || (!isBusy() && !s.messages.slice(i + 1).some(x => x.kind && x.kind !== "problems" && cards[x.kind]));
    const off = problemOffset(s, m);
    run.recommendations.forEach((c, n) => el.append(problemCard(s, m, c, off + n + 1, pickable)));
    if (run.note) el.append(h("p", {class: "info"}, run.note));
    if (active && (run.recommendations.length || !shownProblems(s).length))  // 다 보여 줘서 빈 결과면 다시 눌러도 같다
      el.append(h("div", {class: "row"}, btn("다시 추천 받기", () => startRecommend(s))));
  }

  // ---------- ② 고른 문제 메시지 ----------
  function renderProblem(el, s, m, active) {
    const run = getRun(m.run_id);
    if (!run) return el.append(loading(m.run_id));
    const c = run.recommendations.find(x => x.problem_id === m.problem_id);
    el.append(h("p", {}, "좋습니다. 이 문제로 연구 주제 후보를 만들겠습니다."),
      c ? h("div", {class: "card quiet"}, h("div", {class: "card-title"}, c.title), h("p", {}, c.summary_ko || c.summary), sourceLine(c),
            h("details", {}, h("summary", {}, "원문 인용"), h("blockquote", {}, c.quote))) : h("p", {}, m.problem_id),
      h("p", {}, "보유 GPU, 쓸 수 있는 데이터셋, 기간처럼 고려할 조건이 있으면 먼저 아래 입력창으로 알려 주세요. " +
                 "대화 내용도 참고문헌 검색과 후보 생성에 반영됩니다. 준비되면 버튼을 누르거나 \"후보 만들어 줘\"라고 말해 주세요."));
    if (active) el.append(h("div", {class: "row"}, btn("참고문헌 검색하고 후보 만들기", () => startResearch(s), "primary"),
                            h("span", {class: "caption"}, "수 분 걸립니다")));
  }

  // ---------- ③ 후보 메시지 ----------
  function topicCard(s, m, t, n, active) {
    const picked = m.picked === t.id;
    const verified = s.messages.some(v => v.kind === "verify" && v.research_run_id === m.run_id && v.topic_id === t.id);
    const rev = currentRevision(s, m.run_id, t.id);  // 카드는 원본(v1)을 보이고, 버튼은 고친 버전으로 진행한다
    return h("div", {class: "card" + (picked ? " picked" : "")},
      h("div", {class: "card-title"}, h("span", {class: "num"}, n), t.title,
        picked ? h("span", {class: "badge"}, "선택함") : "", verified ? h("span", {class: "badge gray"}, "검증함") : "",
        rev ? h("span", {class: "badge gray", title: "아래는 원본(v1)입니다. 고친 내용은 수정안 카드에 있습니다."}, `v${rev.version}로 고침`) : ""),
      t.summary ? h("p", {}, ko(t, "summary")) : "",
      h("details", {}, h("summary", {}, "자세히"),
        ["gap", "approach", "evaluation"].filter(k => t[k]).map(k => h("p", {}, h("b", {}, `${TOPIC_LABEL[k]}: `), ko(t, k))),
        t.support ? h("p", {class: "caption"}, `근거 참고문헌: ${t.support.join(", ")}`) : ""),
      active ? h("div", {class: "row"}, btn(rev ? `v${rev.version} 검증` : "이 후보 검증", () => pickTopic(s, m, t, n, "verify"), "primary"),
                 btn("이 후보 더 다듬기", () => pickTopic(s, m, t, n, "chat"))) : "",
      extras("topic", s, t, m));
  }

  function pickTopic(s, m, t, n, mode, {echo = true} = {}) {  // echo: 채팅으로 고르면 사용자 말이 이미 대화에 있다
    updateSession(s.id, x => {
      const rev = currentRevision(x, m.run_id, t.id);
      mark(x, m, "picked", t.id);
      keepPicks(x, "research_run_id", {research_run_id: m.run_id, topic_id: t.id, ...(rev ? {revision_run_id: rev.run_id} : {})});
      x.stage = "refine";
      if (mode === "verify" && echo) say(x, "user", `${n}번 후보를 검증해 주세요: ${t.title}` + (rev ? ` (v${rev.version})` : ""));
    });
    if (mode === "verify") startJob("verify", find(s.id));
    else if (mode === "chat") startChat(find(s.id), {question: `${n}번 후보를 더 구체화하고 싶어요: ${t.title}`});
  }

  function renderTopics(el, s, m, active) {
    const run = getRun(m.run_id);
    if (!run) return el.append(loading(m.run_id));
    if (m.repost) el.append(mdBlock(m.content));  // 검증 뒤 "다른 후보 검증": 설명은 위에 있으니 카드만 다시 보인다
    else el.append(mdBlock(run.presentation),
      h("details", {}, h("summary", {}, `검색한 참고문헌 ${run.references.length}편 · 검색 과정`),
        run.trace ? h("pre", {}, JSON.stringify(run.trace, null, 1)) : "",
        h("ul", {}, run.references.map(r => h("li", {}, `[${r.id}] `,
          safeUrl(r.url) ? h("a", {href: safeUrl(r.url), target: "_blank", rel: "noopener"}, r.title) : r.title,
          ` (${r.year ?? "연도 없음"})`)))));
    el.append(h("p", {class: "caption"}, "기존 연구와 겹치는지는 아직 확인하지 않았습니다. 후보를 검증하면 가장 가까운 논문과 비교합니다."));
    run.topics.forEach((t, i) => el.append(topicCard(s, m, t, i + 1, active)));
    if (active && !m.repost)
      el.append(h("div", {class: "row"}, btn("지금까지 대화를 반영해 후보 다시 만들기", () => startResearch(s))));
  }

  // ---------- 화면 그리기 ----------
  const ICON = {
    edit: '<svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 20h9"/><path d="M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4Z"/></svg>',
    trash: '<svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 6h18"/><path d="M8 6V4h8v2"/><path d="M19 6l-1 14H6L5 6"/></svg>',
    send: '<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><path d="M12 19V5"/><path d="m5 12 7-7 7 7"/></svg>',
    stop: '<svg viewBox="0 0 24 24" width="14" height="14"><rect x="5" y="5" width="14" height="14" rx="2" fill="currentColor"/></svg>',
  };
  function iconBtn(title, icon, onclick) {
    const b = h("button", {type: "button", class: "icon-btn", title, "aria-label": title, onclick});
    b.innerHTML = icon;
    return b;
  }

  function renderAccount() {
    const el = $("#account"), A = window.Auth;
    if (!el) return;
    const kids = [];
    if (account)
      kids.push(h("div", {class: "who-row"}, h("span", {class: "user-dot"}, (account.name || account.email || "?").slice(0, 1)),
                  h("div", {class: "who-text", title: account.email || ""}, h("div", {}, account.name || account.email),
                    h("div", {class: "caption"}, "대화가 계정에 저장됩니다"))),
                h("div", {class: "row"}, btn("로그아웃", () => A.signOut(), "ghost small"),
                  btn("계정 삭제", deleteAccount, "ghost small danger")));
    else if (A && A.enabled())
      kids.push(btn("Google로 로그인", () => A.signIn(), "primary wide"),
                h("div", {class: "caption"}, "로그인하지 않으면 대화는 이 탭에만 저장되고 탭을 닫으면 사라집니다."));
    if (notice) kids.push(h("p", {class: "err"}, notice));
    el.replaceChildren(...kids);
  }

  function renderStub(s) {  // 아직 전체를 받지 않은 세션
    const err = fullErrors[s.id];
    $("#messages").replaceChildren(err
      ? h("p", {class: "err"}, `세션을 불러오지 못했습니다: ${err} `,
          btn("다시 불러오기", () => { delete fullErrors[s.id]; render(); }, "ghost"))
      : h("p", {class: "caption center"}, "세션을 불러오는 중..."));
    $("#stages").replaceChildren();
    $("#topbar-title").textContent = s.title;
    $("#chat-form").hidden = true;
    loadFull(s.id);
  }

  function renderSessions() {
    $("#session-list").replaceChildren(...sessions.map(s => h("li", {class: s.id === currentId ? "current" : ""},
      h("a", {href: "#", title: s.title, onclick: e => { e.preventDefault(); openSession(s.id); }},
        s.title, job && job.sid === s.id ? h("span", {class: "live", title: "진행 중"}) : ""),
      h("span", {class: "item-actions"}, iconBtn("이름 바꾸기", ICON.edit, () => renameSession(s)),
        iconBtn("삭제", ICON.trash, () => removeSession(s))))));
  }

  function renderStates(s) {  // 진행 표시만 한다. 눌러서 이동하지 않는다 (이슈 #15)
    const started = s.messages.some(m => m.kind === "problems");
    const cur = STAGES.findIndex(([k]) => k === s.stage);
    $("#stages").replaceChildren(...STAGES.map(([key, text], i) => {
      const state = !started ? "todo" : i < cur ? "done" : i === cur ? "current" : "todo";
      const summary = state !== "todo" && stageSummary[key] ? stageSummary[key](s) : "";
      return h("li", {class: state}, h("span", {class: "dot"}, state === "done" ? "✓" : String(i + 1)),
               h("div", {}, h("div", {class: "label"}, text.slice(2)), summary ? h("div", {class: "caption"}, summary) : ""));
    }));
    $("#activity").textContent = isBusy() ? job.activity : "";
  }

  function messageEl(s, m, active, retryable) {
    if (m.role === "user") return h("div", {class: "msg user"}, h("div", {class: "bubble"}, m.content));
    const failed = m.status === "cancelled" || m.status === "error";
    const body = h("div", {class: "body"}), card = !failed && m.kind && cards[m.kind];
    if (card) card(body, s, m, active);
    else if (m.content) body.append(mdBlock(m.content));
    if (failed) body.append(h("div", {class: "status"}, m.note || (m.status === "cancelled" ? "중단됨" : "실패"), " ",
                              retryable && jobs[m.job] ? btn("재시도", () => retry(s, m), "small") : ""));
    return h("div", {class: `msg assistant ${m.status}`, "data-id": m.id}, avatar(), body);
  }

  function renderMessages(s) {
    const last = cardIndex(s), busy = isBusy();
    const kids = [welcome(s), ...s.messages.map((m, i) => messageEl(s, m, i === last && !busy, i > last))];
    if (busy) kids.push(jobBubble());
    $("#messages").replaceChildren(...kids);
    const sc = $("#scroll"), key = `${s.id}|${s.messages.length}|${busy ? job.kind : ""}`;
    if (key !== scrollKey) {
      const lastMsg = s.messages[s.messages.length - 1];
      if (!busy && lastMsg && lastMsg.role === "assistant" && lastMsg.kind && scrollKey.startsWith(s.id + "|"))
        scrollTarget = lastMsg;  // 새 결과 카드는 처음부터 읽도록 메시지 위쪽을 맞춘다
      else { scrollTarget = null; stickBottom = true; }
      scrollKey = key;
    }
    if (stickBottom) {  // 세션을 열었거나 새 메시지가 붙으면 맨 아래로. 결과를 불러오는 동안 높이가 바뀌니 다 불러올 때까지
      sc.scrollTop = sc.scrollHeight;
      if (!Object.values(runCache).includes("loading")) stickBottom = false;
    }
    if (!scrollTarget) return;
    const el = $(`#messages [data-id="${scrollTarget.id}"]`);
    if (el) sc.scrollTop += el.getBoundingClientRect().top - sc.getBoundingClientRect().top - 16;
    const r = runCache[scrollTarget.run_id];  // 결과를 다 불러와 높이가 정해질 때까지 맞춘다
    if (!el || !scrollTarget.run_id || (r && r !== "loading")) scrollTarget = null;
  }

  function renderComposer(s) {
    const input = $("#chat-input"), send = $("#send"), ready = !!s.profile, busy = isBusy();
    $("#chat-form").hidden = false;
    input.disabled = !ready || busy;
    input.placeholder = !ready ? "먼저 위에서 연구실 정보를 입력해 주세요"
      : busy ? "작업이 끝나면 입력할 수 있습니다" : "질문하거나 연구 조건을 알려 주세요";
    send.className = busy ? "send stop" : "send";
    send.type = busy ? "button" : "submit";
    send.title = busy ? "중단" : "보내기";
    send.setAttribute("aria-label", send.title);
    send.innerHTML = busy ? ICON.stop : ICON.send;
    send.onclick = busy ? () => cancelJob() : null;
    if (busy) send.dataset.keep = "1"; else delete send.dataset.keep;
    send.disabled = !busy && (!ready || !input.value.trim());
  }

  function render() {
    let s = getSession();
    if (!s) { s = sessions[0] || newSession(); currentId = s.id; persist(); }
    renderAccount();
    renderSessions();
    if (s.stub) return renderStub(s);
    migrate(s);
    $("#topbar-title").textContent = s.title;
    renderStates(s);
    renderMessages(s);
    renderComposer(s);
    if (isBusy())  // G-2: 작업 중에는 그 세션의 입력을 잠근다
      document.querySelectorAll("main button, main input, main textarea, main select")
        .forEach(x => { if (!x.dataset.keep) x.disabled = true; });
  }

  // ---------- 확장 훅 ----------
  function registerCard(kind, renderFn) { cards[kind] = renderFn; }
  function registerStage(stage, summary) { stageSummary[stage] = summary; }
  function registerJob(kind, start) { jobs[kind] = start; }
  function startJob(kind, s, opts = {}) { if (jobs[kind] && s && !isBusy()) return jobs[kind](s, opts); }
  function registerActions(kind, fn) { actionLists[kind] = fn; }
  function registerAction(name, fn) { actionHandlers[name] = fn; }
  const cardExtras = [];
  function addCardExtra(fn) { cardExtras.push(fn); }
  function extras(kind, s, item, m) { return cardExtras.map(fn => fn(kind, s, item, m)); }

  registerCard("problems", renderProblems);
  registerCard("problem", renderProblem);
  registerCard("topics", renderTopics);
  registerStage("browse", s => {
    const run = getRun(s.picks.recommend_run_id);
    if (!run) return "";
    return run.recommendations.length ? `추천 ${run.recommendations.length}개` : "관련 문제 없음";
  });
  registerStage("explore", s => getRun(s.picks.recommend_run_id)?.recommendations
    .find(c => c.problem_id === s.picks.problem_id)?.title || "");
  registerStage("refine", s => {
    const run = getRun(s.picks.research_run_id);
    const topic = run?.topics.find(t => t.id === s.picks.topic_id);
    const rev = topic && currentRevision(s, s.picks.research_run_id, topic.id);
    return run ? `후보 ${run.topics.length}개` + (topic ? ` · 선택: ${topic.title}` + (rev ? ` (v${rev.version})` : "") : "") : "";
  });
  registerJob("recommend", startRecommend);
  registerJob("research", startResearch);
  registerJob("chat", startChat);

  window.App = {STAGES, registerCard, registerStage, registerJob, startJob, registerActions, registerAction, addCardExtra,
                runTask, getSession, updateSession, getRun, loading, isBusy, render, h, md, mdBlock, msg, btn,
                currentRevision, pickTopic, ko};

  document.addEventListener("DOMContentLoaded", () => {
    $("#new-session").onclick = () => openSession(newSession().id);
    const input = $("#chat-input");
    const fit = () => { input.style.height = "auto"; input.style.height = Math.min(input.scrollHeight, 200) + "px"; };
    $("#chat-form").onsubmit = e => {
      e.preventDefault();
      const s = getSession(), text = input.value.trim();
      if (!s || !s.profile || !text || isBusy()) return;
      input.value = "";
      fit();
      startChat(s, {question: text});
    };
    input.oninput = () => {
      fit();
      if (!isBusy()) $("#send").disabled = !input.value.trim();
    };
    input.onkeydown = e => {
      if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); $("#chat-form").requestSubmit(); }
    };
    addEventListener("beforeunload", () => {  // 새로고침·탭 닫기도 취소로 남긴다 (G-4)
      if (!job) return;
      const j = job;
      cancelJob(true);
      const s = find(j.sid);
      if (!s) return;
      place(s, failure(j, "cancelled", {}), j.replace);
      persist();
      if (account && versions[s.id] !== undefined)  // 페이지가 닫혀도 보내지도록 keepalive. 실패하면 중단 기록만 빠진다
        fetch(`/api/sessions/${encodeURIComponent(s.id)}`, {method: "PUT", headers: JSON_HDR, keepalive: true,
          body: JSON.stringify({session: s, base_updated_at: versions[s.id]})}).catch(() => null);
    });
    const A = window.Auth;
    (A ? A.ready : Promise.resolve(null)).catch(() => null).then(async user => {
      account = user;
      await loadSessions();
      if (A && A.error()) notice = A.error();
      render();
      if (A) A.onChange(switchAccount);
    });
  });
})();
