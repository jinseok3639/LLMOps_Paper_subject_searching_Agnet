/* 로그인 (API 명세 4장). Supabase Auth의 Google 로그인. app.js보다 먼저 실행된다.

   window.Auth
     ready            Promise<user|null>. 처음 열 때 /api/config → supabase-js 준비 → /api/me 확인.
     user()           현재 사용자 {id, email, name} 또는 null
     error()          로그인에 실패하고 돌아왔으면 그 안내 문구, 아니면 null
     enabled()        로그인을 쓸 수 있는지 (서버에 Supabase 설정이 있는지)
     signIn()         Google 로그인 페이지로 이동했다가 이 페이지로 돌아온다
     signOut()        로그아웃 (A-3)
     onChange(fn)     ready 이후 사용자가 바뀌면 fn(user|null)
   같은 출처의 /api/ 요청에는 로그인했을 때 Authorization: Bearer <access token>을 자동으로 붙인다.
   토큰은 supabase-js가 만료 전에 새로 받는다.
*/
(() => {
  const rawFetch = window.fetch.bind(window);
  let client = null, user = null;
  const listeners = [];

  async function token() {
    if (!client) return null;
    const {data} = await client.auth.getSession();
    return data.session?.access_token || null;
  }

  window.fetch = async (url, opts = {}) => {
    if (client && typeof url === "string" && url.startsWith("/api/")) {
      const t = await token();
      if (t) opts = {...opts, headers: {...(opts.headers || {}), Authorization: `Bearer ${t}`}};
    }
    return rawFetch(url, opts);
  };

  async function me() {
    const res = await fetch("/api/me");
    return res.ok ? (await res.json()).user : null;
  }

  function takeLoginError() {  // 로그인 실패로 돌아오면 주소에 error_description이 붙는다. 읽고 주소에서 지운다
    const q = new URLSearchParams(location.search), hash = new URLSearchParams(location.hash.slice(1));
    const text = q.get("error_description") || hash.get("error_description");
    if (!text && !q.get("error")) return null;
    history.replaceState(null, "", location.pathname);
    return `로그인 실패: ${text || q.get("error")}`;
  }
  const loginError = takeLoginError();

  async function init() {
    const cfg = await rawFetch("/api/config").then(r => r.json()).catch(() => ({}));
    if (!cfg.supabase_url || !window.supabase) return null;
    client = window.supabase.createClient(cfg.supabase_url, cfg.supabase_anon_key);
    user = await me().catch(() => null);
    client.auth.onAuthStateChange(event => {
      if (!["SIGNED_IN", "SIGNED_OUT", "USER_UPDATED"].includes(event)) return;
      setTimeout(async () => {  // 콜백 안에서 supabase 호출을 기다리면 멈출 수 있어 밖에서 확인한다
        const u = event === "SIGNED_OUT" ? null : await me().catch(() => null);
        if ((u && u.id) === (user && user.id)) return;
        user = u;
        listeners.forEach(fn => fn(user));
      });
    });
    return user;
  }

  window.Auth = {
    ready: init(),
    user: () => user,
    error: () => loginError,
    enabled: () => !!client,
    signIn: () => client.auth.signInWithOAuth({provider: "google",
                                                options: {redirectTo: location.origin + location.pathname}}),
    signOut: () => client.auth.signOut(),
    onChange: fn => listeners.push(fn),
  };
})();
