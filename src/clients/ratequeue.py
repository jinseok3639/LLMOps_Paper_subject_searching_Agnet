"""호출 제한이 있는 외부 API용 프로세스 안 대기열 (S2, arXiv).

서버는 프로세스 하나(--workers 1)로 돌기 때문에 모듈에 둔 대기열 하나로 모든 사용자 요청을 묶을 수 있다.
워커나 인스턴스를 늘리면 외부 큐가 필요하다.
"""
import threading
import time


class RateQueue:
    """번호표 순서로 하나씩 통과시킨다. 통과 시각 사이에 interval초를 두고, 쉬는 중(pause)이면 그 시각까지 기다린다."""

    def __init__(self):
        self._cond = threading.Condition()
        self._next = 0        # 다음에 줄 번호표
        self._serving = 0     # 지금 통과할 번호표
        self._last = float("-inf")  # 마지막 통과 시각 (monotonic)
        self._pause_until = 0.0

    def waiting(self) -> int:
        """줄 선 요청 수 (통과 중인 것 포함)."""
        with self._cond:
            return self._next - self._serving

    def pass_through(self, interval: float) -> None:
        with self._cond:
            ticket = self._next
            self._next += 1
            while self._serving != ticket:
                self._cond.wait()
        try:
            while True:  # 내 차례. 기다리는 동안 쉬는 시각이 뒤로 밀릴 수 있어 다시 확인한다
                with self._cond:
                    wait = max(self._last + interval, self._pause_until) - time.monotonic()
                    if wait <= 0:
                        self._last = time.monotonic()
                        return
                time.sleep(wait)
        finally:
            with self._cond:
                self._serving += 1
                self._cond.notify_all()

    def pause(self, seconds: float) -> None:
        """줄 전체를 seconds초 쉬게 한다 (429, 5xx를 받았을 때)."""
        with self._cond:
            self._pause_until = max(self._pause_until, time.monotonic() + seconds)
