"""FastAPI 서버: src/api/의 라우터와 static/ 화면을 제공한다.

- 라우터: src/api/<기능>.py 중 `router`가 있는 모듈을 모두 등록한다. 기능을 더할 때 이 파일은 고치지 않는다.
- 화면 모듈: static/modules/*.js를 index.html의 `<!-- modules -->` 자리에 이름순으로 넣는다 (app.js 다음에 실행).

실행: python -m uvicorn server:app --port 8000
"""
import importlib
import pkgutil
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException

import src.api
from src import runs
from src.api.common import ApiError

STATIC_DIR = Path(__file__).resolve().parent / "static"


@asynccontextmanager
async def lifespan(app):
    runs.cleanup()  # 24시간 지난 비로그인 결과 삭제 (API 명세 6.5)
    yield


app = FastAPI(title="연구 주제 탐색", lifespan=lifespan)

for info in sorted(pkgutil.iter_modules(src.api.__path__), key=lambda m: m.name):
    module = importlib.import_module(f"src.api.{info.name}")
    if hasattr(module, "router"):
        app.include_router(module.router)


def _error(status: int, code: str, message: str, **extra) -> JSONResponse:
    return JSONResponse({"error": {"code": code, "message": message, "retryable": False, **extra}}, status)


@app.exception_handler(ApiError)
async def _api_error(request, exc: ApiError):
    return JSONResponse(exc.body, exc.status)


@app.exception_handler(RequestValidationError)
async def _validation_error(request, exc: RequestValidationError):
    fields = list(dict.fromkeys(str(e["loc"][-1]) for e in exc.errors() if e.get("loc")))
    return _error(422, "validation_error", "필수 항목을 입력해 주세요.", fields=fields)


@app.exception_handler(HTTPException)
async def _http_error(request, exc: HTTPException):
    return _error(exc.status_code, "not_found" if exc.status_code == 404 else "http_error", str(exc.detail))


@app.get("/", response_class=HTMLResponse)
def index():
    html = (STATIC_DIR / "index.html").read_text("utf-8")
    tags = "\n".join(f'<script defer src="/modules/{p.name}"></script>'
                     for p in sorted((STATIC_DIR / "modules").glob("*.js")))
    return html.replace("<!-- modules -->", tags)


app.mount("/", StaticFiles(directory=STATIC_DIR), name="static")
