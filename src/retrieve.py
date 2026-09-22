"""하이브리드 검색: BM25(키워드) + Dense(의미) + RRF(융합).

기본 동작이 하이브리드다. 옵션이 아니다.

  - BM25는 "관혈적", "제5조" 같은 정확한 용어·조문번호에 강하다.
    단어가 겹치지 않으면 못 찾는다.
  - Dense는 "보험금을 안 주는 경우"로 "면책사유"를 찾는다.
    대신 희소 토큰을 놓친다.
  - 사각지대가 반대라서 RRF(순위 기반 융합)로 합친다.
    점수 스케일이 다른 검색기를 더하면 안 되므로 순위를 쓴다.

임베딩 인덱스는 work/embed_index.npz에 캐시한다. clauses.json의 해시를
함께 저장해, 약관이나 파싱 결과가 바뀐 경우에만 다시 계산한다.
이 캐시는 검색 전처리 캐시이며, --use-cache가 다루는 LLM 응답 캐시와
전혀 다른 것이다.

임베딩 모델이 로드되지 않으면 조용히 BM25로 내려가지 않는다.
검색 품질이 달라진 것을 모른 채 결과를 믿게 되기 때문이다.
"""
import hashlib
import json
import math
import re
import sys
import time
from collections import Counter
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import config as C                                        # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
CLAUSES = ROOT / "work" / "clauses.json"
INDEX = ROOT / "work" / "embed_index.npz"

K1, B = 1.5, 0.75


class RetrievalError(RuntimeError):
    pass


def tokenize(text):
    """한국어 조사 처리를 대신해 어절과 2글자 n-gram을 같이 쓴다."""
    words = re.findall(r"[가-힣A-Za-z0-9]+", text)
    toks = list(words)
    for w in words:
        if len(w) > 2:
            toks += [w[i:i + 2] for i in range(len(w) - 1)]
    return toks


class BM25:
    def __init__(self, corpus):
        self.docs = [tokenize(t) for t in corpus]
        self.n = len(self.docs)
        self.avgdl = sum(len(d) for d in self.docs) / max(self.n, 1)
        df = Counter()
        for d in self.docs:
            df.update(set(d))
        self.idf = {t: math.log(1 + (self.n - c + 0.5) / (c + 0.5))
                    for t, c in df.items()}
        self.tf = [Counter(d) for d in self.docs]

    def search(self, query, top_k):
        q = tokenize(query)
        scored = []
        for i, tf in enumerate(self.tf):
            dl = len(self.docs[i])
            s = 0.0
            for t in q:
                f = tf.get(t)
                if not f:
                    continue
                s += self.idf.get(t, 0.0) * f * (K1 + 1) / (
                    f + K1 * (1 - B + B * dl / self.avgdl))
            if s > 0:
                scored.append((i, s))
        scored.sort(key=lambda x: -x[1])
        return [i for i, _ in scored[:top_k]]


class Encoder:
    """임베딩 백엔드. 설정으로 교체한다."""

    def __init__(self):
        self.backend = C.EMBED_BACKEND
        self.name = C.EMBED_MODEL
        if self.backend == "fake":
            # 인덱스 캐시 로직 단위시험 전용.
            # 의미 검색이 아니므로 실제 하이브리드 검증에 쓰지 말 것.
            self.name = "fake-hash-encoder"
            self.model = None
            return
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError as e:
            raise RetrievalError(
                "임베딩 모델을 로드할 수 없습니다: sentence-transformers 미설치\n"
                "  pip install sentence-transformers\n"
                "  하이브리드 검색이 기본이므로 BM25 단독으로 내려가지 않습니다."
            ) from e
        try:
            self.model = SentenceTransformer(self.name)
        except Exception as e:
            raise RetrievalError(
                f"임베딩 모델 '{self.name}' 로드 실패: {e}\n"
                "  EMBED_MODEL 환경변수로 다른 모델을 지정할 수 있습니다."
            ) from e

    def encode(self, texts):
        if self.backend == "fake":
            out = np.zeros((len(texts), 64), dtype=np.float32)
            for i, t in enumerate(texts):
                for tok in tokenize(t):
                    h = int(hashlib.md5(tok.encode()).hexdigest()[:8], 16)
                    out[i, h % 64] += 1.0
            norm = np.linalg.norm(out, axis=1, keepdims=True)
            return out / np.maximum(norm, 1e-9)
        return np.asarray(
            self.model.encode(texts, normalize_embeddings=True,
                              show_progress_bar=False),
            dtype=np.float32)


def clauses_hash(clauses):
    blob = json.dumps([c["id"] + c["text"] for c in clauses],
                      ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()[:16]


class Retriever:
    def __init__(self, clauses=None, log=print):
        self.log = log
        self.clauses = clauses or json.loads(
            CLAUSES.read_text(encoding="utf-8"))
        self.corpus = [f"{c['path']} {c['text']}" for c in self.clauses]
        self.bm25 = BM25(self.corpus)
        self.encoder = Encoder()
        self.emb, self.reused = self._load_or_build()

    # ── 임베딩 인덱스 캐시 ────────────────────────────────
    def _load_or_build(self):
        h = clauses_hash(self.clauses)
        if INDEX.exists():
            data = np.load(INDEX, allow_pickle=False)
            same_src = str(data["hash"]) == h
            same_model = str(data["model"]) == self.encoder.name
            if same_src and same_model:
                self.log(f"[검색] 임베딩 인덱스 재사용 "
                         f"(hash {h}, model {self.encoder.name})")
                return data["emb"], True
            why = "약관 변경" if not same_src else "모델 변경"
            self.log(f"[검색] 임베딩 인덱스 재생성 ({why})")

        t0 = time.time()
        emb = self.encoder.encode(self.corpus)
        INDEX.parent.mkdir(exist_ok=True)
        np.savez(INDEX, emb=emb, hash=h, model=self.encoder.name)
        self.log(f"[검색] 임베딩 인덱스 생성 {emb.shape[0]}건 "
                 f"({time.time() - t0:.1f}초, model {self.encoder.name})")
        return emb, False

    # ── 검색 ──────────────────────────────────────────────
    def _dense(self, query, top_k):
        q = self.encoder.encode([query])[0]
        sims = self.emb @ q
        idx = np.argsort(-sims)[:top_k]
        return [int(i) for i in idx]

    def search(self, query, top_k=None, verbose=False):
        """BM25와 Dense를 각각 돌린 뒤 RRF로 합친다.

        결과 조각에는 어떤 검색기가 몇 위로 올렸는지를 붙여서 돌려준다.
        Verify가 원문 대조를 할 수 있도록 clause 원본 필드는 그대로 둔다.
        """
        top_k = top_k or C.RRF_TOP_K
        a = self.bm25.search(query, C.BM25_TOP_K)
        b = self._dense(query, C.DENSE_TOP_K)

        score, rank_of = {}, {}
        for method, ranking in (("bm25", a), ("dense", b)):
            for rank, idx in enumerate(ranking):
                score[idx] = score.get(idx, 0.0) + 1.0 / (C.RRF_K + rank + 1)
                rank_of.setdefault(idx, {})[method] = rank + 1

        merged = sorted(score.items(), key=lambda x: -x[1])[:top_k]
        if verbose:
            self.log(f"[검색] BM25 top-k = {len(a)}")
            self.log(f"[검색] Embedding top-k = {len(b)}")
            self.log(f"[검색] RRF fusion = {len(merged)}")
            self.log("[검색] Hybrid Retrieval 완료")

        out = []
        for final_rank, (idx, s) in enumerate(merged, start=1):
            c = dict(self.clauses[idx])
            methods = rank_of.get(idx, {})
            c["retrieval"] = {
                "method": "+".join(sorted(methods)) or "rrf",
                "bm25_rank": methods.get("bm25"),
                "dense_rank": methods.get("dense"),
                "rrf_score": round(s, 5),
                "rank": final_rank,
            }
            out.append(c)
        return out

    def by_id(self, cid):
        for c in self.clauses:
            if c["id"] == cid:
                return c
        return None


if __name__ == "__main__":
    r = Retriever()
    print(f"[검색] 조각 {len(r.clauses)}개 / 백엔드 {r.encoder.backend} "
          f"/ 인덱스 {'재사용' if r.reused else '신규'}")
    for q in ["보험금을 안 주는 경우", "재입원하면 한도가 다시 시작되나",
              "관혈적"]:
        print(f"\n  질의: {q}")
        for c in r.search(q, top_k=3, verbose=True):
            rt = c["retrieval"]
            print(f"    [{rt['rank']}] {c['id']}  "
                  f"({rt['method']}, bm25={rt['bm25_rank']}, "
                  f"dense={rt['dense_rank']})")
            print(f"        {c['text'][:52]}")
