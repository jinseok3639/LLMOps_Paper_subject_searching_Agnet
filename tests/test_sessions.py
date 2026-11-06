"""로그인과 세션 저장 테스트 (API 명세 4, 5장). Supabase는 모의 처리하고 저장은 임시 폴더(LocalStore)에 한다.
실행: python -m unittest discover -s tests"""
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

import server
from src import auth, calllog, config, runs, store
from src.api import sse

USERS = {"tok-a": {"id": "11111111-aaaa", "email": "a@example.com", "name": "A"},
         "tok-b": {"id": "22222222-bbbb", "email": "b@example.com", "name": "B"}}
A, B = {"Authorization": "Bearer tok-a"}, {"Authorization": "Bearer tok-b"}


def session(sid="s_0123456789abcdef", **extra):
    return {"id": sid, "title": "새 대화", "created_at": "2026-11-06T00:00:00Z", "updated_at": "2026-11-06T00:00:00Z",
            "profile": None, "stage": "browse", "picks": {}, "messages": [], "rejections": [], **extra}


class Base(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(dir=config.DB_DIR)
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        for p in (patch.object(store, "backend", store.LocalStore(root / "users")),
                  patch.object(runs, "RUNS_DIR", root / "runs"),
                  patch.object(calllog, "ROOT", root),
                  patch.object(auth, "verify_token", lambda t: USERS.get(t)),
                  patch.object(auth, "_cache", {}),
                  patch.dict(os.environ, {"SUPABASE_URL": "https://x.supabase.co", "SUPABASE_ANON_KEY": "anon"})):
            p.start()
            self.addCleanup(p.stop)
        self.client = TestClient(server.app)

    def put(self, s, base=None, headers=A):
        return self.client.put(f"/api/sessions/{s['id']}", json={"session": s, "base_updated_at": base}, headers=headers)


class AuthTests(Base):
    def test_config_and_me(self):
        self.assertEqual(self.client.get("/api/config").json(),
                         {"supabase_url": "https://x.supabase.co", "supabase_anon_key": "anon"})
        self.assertEqual(self.client.get("/api/me").json(), {"user": None})
        self.assertEqual(self.client.get("/api/me", headers=A).json()["user"]["email"], "a@example.com")
        res = self.client.get("/api/me", headers={"Authorization": "Bearer wrong"})
        self.assertEqual((res.status_code, res.json()["error"]["code"]), (401, "unauthorized"))

    def test_login_disabled_without_config(self):
        with patch.dict(os.environ, {"SUPABASE_URL": "", "SUPABASE_ANON_KEY": ""}):
            self.assertEqual(self.client.get("/api/config").json()["supabase_url"], None)
            self.assertEqual(self.client.get("/api/me", headers=A).json(), {"user": None})

    def test_session_api_requires_login(self):
        res = self.client.get("/api/sessions")
        self.assertEqual((res.status_code, res.json()["error"]["code"]), (401, "unauthorized"))


class SessionTests(Base):
    def test_create_list_get_update_delete(self):
        res = self.put(session())
        self.assertEqual(res.status_code, 200)
        v1 = res.json()["updated_at"]
        self.assertEqual([x["id"] for x in self.client.get("/api/sessions", headers=A).json()], ["s_0123456789abcdef"])

        s = session(title="바뀐 제목", messages=[{"id": "m_1", "role": "user", "content": "안녕", "status": "done"}])
        v2 = self.put(s, v1).json()["updated_at"]
        got = self.client.get("/api/sessions/s_0123456789abcdef", headers=A).json()
        self.assertEqual((got["title"], got["updated_at"], len(got["messages"])), ("바뀐 제목", v2, 1))

        self.assertEqual(self.client.delete("/api/sessions/s_0123456789abcdef", headers=A).status_code, 204)
        self.assertEqual(self.client.get("/api/sessions/s_0123456789abcdef", headers=A).status_code, 404)

    def test_version_conflict_returns_current(self):
        v1 = self.put(session()).json()["updated_at"]
        self.put(session(title="탭 1"), v1)
        res = self.put(session(title="탭 2"), v1)  # 탭 2는 옛 버전을 들고 있다
        self.assertEqual((res.status_code, res.json()["error"]["code"]), (409, "version_conflict"))
        self.assertEqual(res.json()["error"]["current"]["title"], "탭 1")
        self.assertEqual(self.put(session()).status_code, 409)  # 이미 있는 id를 새 세션으로 저장

    def test_other_users_session_is_not_found(self):
        self.put(session())
        self.assertEqual(self.client.get("/api/sessions/s_0123456789abcdef", headers=B).status_code, 404)
        self.assertEqual(self.client.delete("/api/sessions/s_0123456789abcdef", headers=B).status_code, 404)
        self.assertEqual(self.client.get("/api/sessions", headers=B).json(), [])

    def test_rejects_bad_id(self):
        self.assertEqual(self.put(session("s_0123456789abcdef"), headers=A).status_code, 200)
        res = self.client.put("/api/sessions/s_aaaaaaaaaaaaaaaa", json={"session": session()}, headers=A)
        self.assertEqual(res.status_code, 422)

    def test_profile(self):
        self.assertIsNone(self.client.get("/api/profile", headers=A).json())
        p = {"lab_name": " 연구실 ", "lab_topics": "AI 보안", "interests": ""}
        self.assertEqual(self.client.put("/api/profile", json=p, headers=A).json()["lab_name"], "연구실")
        self.assertEqual(self.client.get("/api/profile", headers=A).json()["lab_topics"], "AI 보안")
        self.assertIsNone(self.client.get("/api/profile", headers=B).json())

    def test_import_moves_anonymous_runs(self):
        run_id = runs.save("recommend", {"status": "ok"}, "s_0123456789abcdef")  # 비로그인 결과
        self.put(session("s_00000000000000aa"))
        body = {"sessions": [session(picks={"recommend_run_id": run_id}),
                             session("s_00000000000000aa", title="같은 id")]}
        imported = self.client.post("/api/sessions/import", json=body, headers=A).json()["imported"]
        self.assertEqual(imported[0], {"old_id": "s_0123456789abcdef", "new_id": "s_0123456789abcdef"})
        self.assertNotEqual(imported[1]["new_id"], "s_00000000000000aa")  # 이미 있던 id는 새로 받는다
        self.assertFalse((runs.RUNS_DIR / f"{run_id}.json").exists())  # 24시간 정리 대상에서 빠졌다
        self.assertEqual(runs.load(run_id)["status"], "ok")

        self.client.delete("/api/sessions/s_0123456789abcdef", headers=A)  # 세션을 지우면 결과도 지운다
        self.assertIsNone(runs.load(run_id))

    def test_delete_account(self):
        self.put(session())
        self.client.put("/api/profile", json={"lab_name": "x", "lab_topics": "y"}, headers=A)
        with patch.object(auth, "delete_auth_user") as deleted:
            self.assertEqual(self.client.delete("/api/me", headers=A).status_code, 204)
        deleted.assert_called_once_with(USERS["tok-a"]["id"])
        self.assertEqual(self.client.get("/api/sessions", headers=A).json(), [])
        self.assertIsNone(self.client.get("/api/profile", headers=A).json())


class JobTests(Base):
    def stream(self, headers, session_id="s_0123456789abcdef", wait=0.0):
        def job(progress, delta):
            time.sleep(wait)
            return {"run_id": runs.save("recommend", {"status": "ok"}, session_id)}

        class Req:  # stream_job이 쓰는 것만
            url = None

            def __init__(self):
                self.headers = {k.lower(): v for k, v in headers.items()}

            async def is_disconnected(self):
                return False

        return sse.stream_job(Req(), session_id, job, kind="recommend")

    def body(self, response):
        import asyncio

        async def read():
            return "".join([c async for c in response.body_iterator])
        return asyncio.run(read())

    def result(self, text):
        block = [b for b in text.split("\n\n") if b.startswith("event: result")][0]
        return json.loads(block.split("data: ", 1)[1])

    def test_logged_in_runs_are_saved_to_store(self):
        run_id = self.result(self.body(self.stream(A)))["run_id"]
        self.assertFalse((runs.RUNS_DIR / f"{run_id}.json").exists())
        self.assertEqual(store.backend.load_run(run_id)["owner_session"], "s_0123456789abcdef")
        anon = self.result(self.body(self.stream({})))["run_id"]
        self.assertTrue((runs.RUNS_DIR / f"{anon}.json").exists())

    def test_same_session_job_is_locked_for_logged_in_user(self):
        first = self.stream(A, wait=0.3)
        from src.api.common import ApiError
        with self.assertRaises(ApiError) as e:
            self.stream(A)
        self.assertEqual(e.exception.body["error"]["code"], "job_running")
        self.body(self.stream(B))  # 다른 사용자는 상관없다
        self.body(first)
        time.sleep(0.05)
        self.body(self.stream(A))  # 끝나면 풀린다


if __name__ == "__main__":
    unittest.main()
