"""단위 OpenProblems 임베딩 (P3 사전 작업).

실행: python -m src.embed [--refresh]

입력: 4_unit_open_problems/all.json
결과: 4_unit_open_problems/embeddings.npy   (N, dim) float32, L2 정규화. all.json의 problems 순서와 같음
      4_unit_open_problems/embeddings.json  {model, generated_at, ids, text_hashes}

all.json의 id와 텍스트가 바뀌지 않았으면 API를 호출하지 않는다.
"""
import argparse
import hashlib
import json
from datetime import datetime, timezone

import numpy as np

from src import config, llm


def passage_text(p: dict) -> str:
    """임베딩할 텍스트: 제목 + 요약 + 키워드."""
    return f"{p['title']}. {p['summary']} Keywords: {', '.join(p['keywords'])}"


def _hash(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:12]


def run(refresh: bool = False) -> None:
    problems = json.loads(config.OPEN_PROBLEMS_PATH.read_text(encoding="utf-8"))["problems"]
    ids = [p["problem_id"] for p in problems]
    texts = [passage_text(p) for p in problems]
    hashes = [_hash(t) for t in texts]

    if not refresh and config.EMBEDDINGS_META_PATH.exists() and config.EMBEDDINGS_PATH.exists():
        meta = json.loads(config.EMBEDDINGS_META_PATH.read_text(encoding="utf-8"))
        if meta["ids"] == ids and meta["text_hashes"] == hashes and meta["model"] == config.EMBED_PASSAGE_MODEL:
            print(f"변경 없음: {len(ids)}개, API 호출 없음")
            return

    vecs = np.array(llm.embed(texts, config.EMBED_PASSAGE_MODEL), dtype=np.float32)
    vecs /= np.linalg.norm(vecs, axis=1, keepdims=True)
    np.save(config.EMBEDDINGS_PATH, vecs)
    meta = {
        "model": config.EMBED_PASSAGE_MODEL,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "ids": ids,
        "text_hashes": hashes,
    }
    config.EMBEDDINGS_META_PATH.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"임베딩 {vecs.shape[0]}개 저장 (dim {vecs.shape[1]})")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--refresh", action="store_true", help="변경이 없어도 다시 임베딩")
    run(refresh=ap.parse_args().refresh)


if __name__ == "__main__":
    main()
