"""하이브리드 검색 실시간 데모.

질의를 입력하면 BM25, Dense, RRF 결과를 나란히 보여준다.
LLM을 부르지 않으므로 빠르고, 발표 중에 즉석 질의로 시연할 수 있다.

사용법: python src/demo_search.py
종료:   빈 줄에서 엔터 또는 q
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import config as C                                        # noqa: E402
from retrieve import Retriever, RetrievalError            # noqa: E402

SHOW = 5  # 화면에 보여줄 순위 수


def short(cid, n=34):
    return cid if len(cid) <= n else cid[:n - 1] + "…"


def show(r, query):
    t0 = time.time()
    bm = r.bm25.search(query, C.BM25_TOP_K)
    t1 = time.time()
    de = r._dense(query, C.DENSE_TOP_K)
    t2 = time.time()
    merged = r.search(query)
    t3 = time.time()

    ids = [c["id"] for c in r.clauses]
    print(f"\n  질의: {query}")
    print(f"  {'BM25 (키워드)':<36}{'Dense (의미)':<36}")
    for i in range(SHOW):
        a = short(ids[bm[i]]) if i < len(bm) else "-"
        b = short(ids[de[i]]) if i < len(de) else "-"
        print(f"  {i + 1}. {a:<33}{i + 1}. {b:<33}")
    print(f"  ({(t1 - t0) * 1000:.0f}ms)"
          f"{'':<29}({(t2 - t1) * 1000:.0f}ms)")

    print(f"\n  RRF 융합 결과 (k={C.RRF_K}, {(t3 - t2) * 1000:.0f}ms)")
    for c in merged[:SHOW]:
        rt = c["retrieval"]
        who = {"bm25": "키워드만", "dense": "의미만",
               "bm25+dense": "둘 다"}.get(rt["method"], rt["method"])
        print(f"  [{rt['rank']}] {c['id']}")
        print(f"      {who:<5} bm25={rt['bm25_rank'] or '-'}  "
              f"dense={rt['dense_rank'] or '-'}  rrf={rt['rrf_score']}")
        print(f"      {c['text'][:56]}")


def main():
    print("[데모] 하이브리드 검색기 로딩 중...")
    try:
        r = Retriever()
    except RetrievalError as e:
        sys.exit(f"[데모] 초기화 실패\n{e}")
    print(f"[데모] 조각 {len(r.clauses)}개 / 모델 {r.encoder.name} "
          f"/ 인덱스 {'재사용' if r.reused else '신규 생성'}")

    while True:
        try:
            q = input("\n질의 > ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not q or q.lower() == "q":
            break
        show(r, q)


if __name__ == "__main__":
    main()
