"""S2 클라이언트의 429 대기 테스트. requests와 대기열은 가짜로 바꾼다."""
import unittest
from unittest.mock import patch

from src.clients import semantic_scholar as s2


class Resp:
    def __init__(self, status=200, body=None, headers=None):
        self.status_code, self._body, self.text, self.headers = status, body, "", headers or {}

    def json(self):
        return self._body


class RetryAfterTests(unittest.TestCase):
    def rests(self, responses):
        """응답을 차례로 돌려주고, 대기열을 쉬게 한 시간 목록을 돌려준다."""
        with patch.object(s2, "_queue") as queue, patch("src.clients.semantic_scholar.requests.get", side_effect=responses):
            s2.get("/paper/search", tries=len(responses))
        return [c.args[0] for c in queue.pause.call_args_list]

    def test_longer_retry_after_wins_over_backoff(self):
        self.assertEqual(self.rests([Resp(429, headers={"Retry-After": "7"}), Resp(body={})]), [7.0])

    def test_shorter_or_missing_retry_after_keeps_backoff(self):
        rests = self.rests([Resp(429, headers={"Retry-After": "1"}), Resp(429),
                            Resp(429, headers={"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"}), Resp(body={})])
        self.assertEqual(rests, [3.0, 6.0, 9.0])

    def test_retry_after_is_capped(self):
        self.assertEqual(self.rests([Resp(429, headers={"Retry-After": "3600"}), Resp(body={})]), [s2.MAX_BACKOFF])


if __name__ == "__main__":
    unittest.main()
