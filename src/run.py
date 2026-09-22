"""예제 3 — 약관 경계사례 탐지 및 테스트 케이스화 에이전트.

워크플로
  STEP 1  모호 표현 탐지                      (LLM)
  STEP 2  Scenario Generator                  (LLM)
  STEP 3  Coverage Critic                     (LLM)
            insufficient → missing dimensions → STEP 2 추가 생성
            기존 사례는 버리지 않고 누적한다. 최대 2회.
  STEP 4  Adjudication Evidence Retriever     (코드, BM25+Dense+RRF)
  STEP 5  Case Critic                         (LLM)
  STEP 6  Code Verify                         (코드)
            STEP 5 또는 6에서 반려 → 재생성 → STEP 4 재검색 → 5 → 6
            시나리오가 바뀌면 근거도 다시 찾아야 하므로 재검색이 필수다.
  STEP 7  Test-case Formatter                 (코드)
  STEP 8  Human Reviewer HTML                 (코드)

이 파일은 모델을 직접 부르지 않는다. 모든 LLM 호출은 agent._call을 거친다.

두 가지 캐시를 구분한다.
  --use-cache         : LLM 응답 재생 (work/cache.json)
  임베딩 인덱스 캐시  : 검색 전처리 (work/embed_index.npz)
"""
import argparse
import copy
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import config as C                                        # noqa: E402
import dimensions as D                                    # noqa: E402
import format_cases as F                                  # noqa: E402
import verify as V                                        # noqa: E402
from retrieve import Retriever, RetrievalError            # noqa: E402

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

WORK = ROOT / "work"
OUT = ROOT / "outputs"
CACHE = WORK / "cache.json"
LOG = WORK / "loop_log.jsonl"


# 화면 출력과 별개로 파일에도 남긴다.
# PowerShell 리다이렉션(> 나 Out-File)을 거치면 cp949로 저장되어 한글이
# 깨지므로, 파이썬이 UTF-8로 직접 쓴다.
_LOG_FILE = None


def set_log_file(path):
    global _LOG_FILE
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    _LOG_FILE = p.open("w", encoding="utf-8")


def _clip(text, n):
    """문장 중간에서 어색하게 끊기지 않도록 어절 경계에서 자른다."""
    text = " ".join(str(text).split())
    if len(text) <= n:
        return text
    cut = text[:n]
    sp = cut.rfind(" ")
    return (cut[:sp] if sp > n * 0.6 else cut) + "…"


def _wrap(text, n):
    """긴 문장을 어절 경계로 나눠 여러 줄로 만든다."""
    words, lines, cur = str(text).split(), [], ""
    for w in words:
        if len(cur) + len(w) + 1 > n:
            lines.append(cur)
            cur = w
        else:
            cur = f"{cur} {w}".strip()
    if cur:
        lines.append(cur)
    return lines


def log(tag, msg, **extra):
    line = f"[{tag}] {msg}"
    print(line)
    if _LOG_FILE:
        _LOG_FILE.write(line + "\n")
        _LOG_FILE.flush()
    with LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"tag": tag, "msg": msg, **extra},
                           ensure_ascii=False) + "\n")


class Cache:
    """LLM 응답 재생용. 임베딩 인덱스 캐시와는 다른 것이다."""

    def __init__(self, replay):
        self.replay = replay
        self.data = {}
        self.calls = 0
        self.resumed = 0
        self.misses = []
        if replay:
            if not CACHE.exists():
                sys.exit("캐시가 없습니다. --use-cache 없이 한 번 실행해주세요.")
            self.data = json.loads(CACHE.read_text(encoding="utf-8"))
        elif CACHE.exists():
            # 이어서 돌린다. 이미 받아둔 응답은 다시 호출하지 않는다.
            self.data = json.loads(CACHE.read_text(encoding="utf-8"))
            self.resumed = len(self.data)

    def get(self, key, produce):
        """항상 복사본을 돌려준다.

        호출자가 레코드에 case_id나 근거를 덧붙이는데, 원본을 그대로
        넘기면 캐시에 저장될 값이 함께 바뀐다. 그러면 재생 시 캐시 키가
        달라져 미스가 난다. 실제로 겪은 버그라 여기서 끊는다.
        """
        if key in self.data and not self.replay:
            return copy.deepcopy(self.data[key])
        if self.replay:
            if key not in self.data:
                self.misses.append(key)
                log("캐시", f"미스 — 이 단계를 건너뜁니다: {key}")
                return []
            return copy.deepcopy(self.data[key])
        value = produce()
        self.data[key] = copy.deepcopy(value)
        self.calls += 1
        # 호출 하나가 끝날 때마다 바로 저장한다.
        # 마지막에 한 번만 쓰면 중간에 끊겼을 때 그때까지 받은 응답이
        # 전부 날아간다. 20분짜리 실행에서는 치명적이다.
        self.save()
        return value

    def save(self):
        if not self.replay:
            CACHE.write_text(json.dumps(self.data, ensure_ascii=False,
                                        indent=2), encoding="utf-8")


def load_clauses():
    path = WORK / "clauses.json"
    if not path.exists():
        sys.exit("먼저 python src/parse_policy.py 를 실행해주세요.")
    return json.loads(path.read_text(encoding="utf-8"))


def retrieve_evidence(retriever, case, verbose):
    """STEP 4 — 생성된 시나리오의 쟁점으로 약관을 다시 검색한다.

    Generator가 인용한 조문을 그대로 믿지 않는다. 시나리오가 바뀌면
    근거도 달라져야 하므로 재생성 때마다 이 단계를 다시 탄다.
    """
    import agent as A
    query = A.evidence_queries(case)
    hits = retriever.search(query, verbose=verbose)
    return query, hits


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--use-cache", action="store_true",
                    help="저장된 LLM 응답으로 재생한다")
    ap.add_argument("--allow-insufficient", action="store_true",
                    help="커버리지 미달이어도 Test Case를 생성한다 "
                         "(기본은 차단)")
    ap.add_argument("--log", metavar="파일",
                    help="실행 로그를 UTF-8 파일로도 저장한다")
    args = ap.parse_args()
    if args.log:
        set_log_file(args.log)

    LOG.unlink(missing_ok=True)
    OUT.mkdir(exist_ok=True)

    clauses = load_clauses()
    clause_index = {c["id"]: c for c in clauses}
    log("파싱", f"조각 {len(clauses)}개 로드")

    dims, dropped = D.active_dimensions([c["id"] for c in clauses])
    dim_names = [d["name"] for d in dims]
    log("차원", f"유효 판단 경계 축 {len(dims)}개: {', '.join(dim_names)}")
    if dropped:
        log("차원", f"이 약관에서 의미 없어 제외: {', '.join(dropped)}")

    try:
        retriever = Retriever(clauses, log=print)
    except RetrievalError as e:
        sys.exit(f"\n[검색] 하이브리드 검색을 초기화하지 못했습니다.\n{e}")

    cache = Cache(args.use_cache)
    if cache.resumed:
        log("캐시", f"이전 실행 응답 {cache.resumed}건을 이어받습니다 "
                    f"(다시 호출하지 않음)")
    import agent as A

    # ── STEP 1 ────────────────────────────────────────────
    terms = cache.get("terms",
                      lambda: A.find_ambiguous_terms(clauses, dims))
    if not terms:
        sys.exit("모호 표현을 한 건도 받지 못했습니다. CLI 동작을 확인해주세요.")
    terms = terms[:C.MAX_TERMS]
    log("탐지", f"모호 표현 {len(terms)}건")
    for t in terms:
        log("탐지", f"  · [{t.get('dimension', '미지정')}] {t.get('term')}")

    # ── STEP 2 · Scenario Generator ───────────────────────
    cases = []
    for t in terms:
        anchors = [retriever.by_id(cid) for cid in (t.get("where") or [])]
        seed = retriever.search(f"{t.get('term')} {t.get('why', '')}")
        pool = seed + [a for a in anchors if a and a not in seed]
        key = f"gen::{t.get('term')}::1"
        got = cache.get(key, lambda t=t, p=pool: A.generate_cases(t, p, dims))
        for r in got:
            r["_term"] = t.get("term")
            r["_pool_ids"] = [c["id"] for c in pool]
        cases += got
        log("Generator", f"'{t.get('term')}' → {len(got)}건 (누적 {len(cases)})")

    # ── STEP 3 · Coverage Critic ──────────────────────────
    # 판단 경계 축과 사례 유형, 두 축을 함께 관리한다.
    # 축만 채워도 전부 경계사례면 테스트 세트로 쓸 수 없다.
    cov, coverage_met = {}, False
    for rnd in range(1, C.MAX_COVERAGE_ROUNDS + 2):
        gap, have = D.type_gap(cases)
        cov = cache.get(f"coverage::{rnd}",
                        lambda c=list(cases), g=dict(gap), h=dict(have):
                        A.coverage_critic(c, dims, g, h))
        # 캐시 미스는 빈 리스트를 돌려준다. dict가 아니면 판정 없음으로 본다.
        if not isinstance(cov, dict):
            cov = {}
        missing_dims = cov.get("missing_dimensions") or []
        # 모델 판정과 별개로 코드가 목표치를 직접 계산한다.
        # 유형 부족은 세면 되는 일이라 모델에게 맡기지 않는다.
        met = (not gap) and (not missing_dims)

        log("Coverage Critic",
            f"{rnd}회차 — {'sufficient' if met else 'insufficient'} / "
            f"축 {len(dims) - len(missing_dims)}/{len(dims)} / "
            f"유형 " + ", ".join(f"{t} {have.get(t, 0)}/{n}"
                                for t, n in D.CASE_TYPE_TARGET.items()),
            gap=gap)
        if cov.get("reason"):
            for line in _wrap(cov["reason"], 78):
                log("Coverage Critic", f"  {line}")

        if met:
            coverage_met = True
            break
        if rnd > C.MAX_COVERAGE_ROUNDS:
            log("Coverage Critic",
                "추가 생성 한도 도달 — 커버리지 미달 상태로 종료")
            if missing_dims:
                log("Coverage Critic", f"  미충족 축: {', '.join(missing_dims)}")
            if gap:
                log("Coverage Critic", "  부족 유형: " +
                    ", ".join(f"{t} {n}건" for t, n in gap.items()))
            log("Coverage Critic",
                "  → 산출물에 커버리지 미달을 표시하고 사람 검토로 넘깁니다")
            break

        if missing_dims:
            log("Coverage Gap", f"미충족 축: {', '.join(missing_dims)}")
        if gap:
            log("Coverage Gap", "부족 유형: " +
                ", ".join(f"{t} {n}건" for t, n in gap.items()))

        existing = [f"[{c.get('coverage_dimension')}/{c.get('case_type')}] "
                    f"{c.get('scenario', '')[:45]}" for c in cases]

        # (1) 비어 있는 축 채우기
        for name in missing_dims:
            spec = next((d for d in dims if d["name"] == name), None)
            if not spec:
                continue
            pool = [retriever.by_id(a) for a in spec["matched_anchors"]]
            pool = [p for p in pool if p]
            pseudo = {"term": f"{name} 관련 판정", "why": spec["desc"],
                      "dimension": name, "where": spec["matched_anchors"]}
            extra = cache.get(
                f"gen_extra::{name}::{rnd}",
                lambda p=pseudo, po=pool, ex=existing:
                A.generate_cases(p, po, dims, missing_dimensions=[name],
                                 existing=ex))
            for r in extra:
                r["_term"] = pseudo["term"]
                r["_pool_ids"] = [c["id"] for c in pool]
            cases += extra
            log("Generator", f"축 보강 [{name}] → {len(extra)}건 "
                             f"(누적 {len(cases)})")

        # (2) 부족한 사례 유형 채우기 — 축이 아니라 유형을 지정해 생성
        if gap:
            anchor_dim = dims[0]
            pool = retriever.search(
                " ".join(t.get("term", "") for t in terms[:3]))
            pseudo = {"term": "사례 유형 보강", "why": "유형 균형 확보",
                      "dimension": anchor_dim["name"],
                      "where": [c["id"] for c in pool[:4]]}
            extra = cache.get(
                f"gen_types::{rnd}",
                lambda p=pseudo, po=pool, g=dict(gap), ex=existing:
                A.generate_cases(p, po, dims, missing_types=g,
                                 existing=ex, n=sum(g.values())))
            for r in extra:
                r["_term"] = pseudo["term"]
                r["_pool_ids"] = [c["id"] for c in pool]
            cases += extra
            log("Generator", "유형 보강 [" +
                ", ".join(f"{t} {n}건" for t, n in gap.items()) +
                f"] → {len(extra)}건 (누적 {len(cases)})")

    # ── STEP 4~6 · 사례별 근거 확보 → 비평 → 검증 ─────────
    passed, rejected = [], []
    state = {"first": True, "seq": 0}

    def materialize_citations(case, candidates):
        """cited_text를 원문에서 주입하고, 후보 밖 인용을 걷어낸다.

        인용 선택 = LLM / 원문 확보 = 코드.

        중요: 후보 집합(candidates) 밖의 조문은 받지 않는다.
        전역 조문 사전으로 떨어뜨리면 Generator가 검색 결과에 없는
        조문을 골라도 통과해버리고, 그 뒤 Case Critic이 "참조 조문에
        없다"며 반려한다. 실제 반려의 70%가 이 경로였다.

        버려진 인용은 _dropped_citations에 남겨 반려 사유에 쓴다.
        """
        cand = {c["id"]: c for c in candidates}
        cites, texts, meta, dropped = [], [], [], []
        for cid in (case.get("cited_clauses") or []):
            src = cand.get(cid)
            if not src:
                dropped.append(cid)
                continue
            cites.append(cid)
            texts.append(src["text"])
            meta.append({"clause_id": cid, "path": src.get("path", ""),
                         "doc": src.get("doc", ""),
                         "cited_text": src["text"],
                         "retrieval": src.get("retrieval", {})})
        case["cited_clauses"] = cites
        case["cited_text"] = texts      # 기존 스키마 유지
        case["_citations"] = meta       # 경로·검색 메타 보존
        case["_dropped_citations"] = dropped
        return dropped

    def process_case(case, label):
        """한 사례를 Evidence → Critic → Verify로 통과시킨다.

        추가 생성분도 같은 경로를 타야 하므로 함수로 분리했다.
        통과하면 레코드를, 실패하면 None을 돌려준다.
        """
        state["seq"] += 1
        case.setdefault("case_id", f"EC-{state['seq']:03d}")
        cur, attempt = case, 1
        while True:
            query, evidence = retrieve_evidence(retriever, cur,
                                                verbose=state["first"])
            state["first"] = False
            tag = "Evidence Retriever" if attempt == 1 else "RE-RETRIEVE"
            log(tag, f"{cur['case_id']}{label} 질의 '{query[:70]}' → 조문 "
                     f"{len(evidence)}개",
                clauses=[e["id"] for e in evidence])
            # 인용 후보 = 재검색 결과 + 생성 시 참조했던 조문.
            # 재검색만으로 좁히면 생성 시점에 본 조문을 인용했다는
            # 이유로 반려되므로, 실제로 모델이 볼 수 있었던 범위를
            # 후보로 삼는다.
            pool_ids = cur.get("_pool_ids") or []
            candidates = list(evidence)
            seen = {e["id"] for e in evidence}
            for pid in pool_ids:
                if pid not in seen and pid in clause_index:
                    candidates.append(clause_index[pid])
                    seen.add(pid)
            cur["_evidence"] = candidates
            dropped = materialize_citations(cur, candidates)
            if dropped:
                log("인용정리", f"{cur['case_id']} 후보 밖 인용 "
                                f"{len(dropped)}건 제거: "
                                f"{', '.join(dropped[:2])}")

            ckey = f"critic::{cur['case_id']}::{attempt}"
            verdicts = cache.get(ckey, lambda c=cur, e=candidates:
                                 A.case_critic([c], e))
            v = next((x for x in verdicts
                      if x.get("case_id") in (cur.get("case_id"),
                                              cur.get("source_case_id"))),
                     verdicts[0] if verdicts else {})
            reasons = []
            if v.get("verdict") == "반려":
                reasons = v.get("reasons") or ["사유 미기재"]
                log("Case Critic", f"{cur['case_id']} REJECT — "
                                   f"{_clip(reasons[0], 76)}")
            else:
                log("Case Critic", f"{cur['case_id']} PASS")
                ok, bad = V.verify_all([cur], clauses, dim_names)
                if ok:
                    log("Code Verify", f"{cur['case_id']} PASS")
                    return cur
                reasons = bad[0]["verify_problems"]
                log("Code Verify",
                    f"{cur['case_id']} REJECT — {reasons[0]}  "
                    f"※ Case Critic은 통과시킴")

            rejected.append(dict(cur, reject_reasons=reasons, attempt=attempt,
                                 rejected_by=("Case Critic"
                                              if v.get("verdict") == "반려"
                                              else "Code Verify")))
            if attempt > C.MAX_REGENERATE:
                log("보류", f"{cur['case_id']} 재생성 한도 초과, 폐기")
                return None

            log("REGENERATE", f"{cur['case_id']} 반려 사유 반영해 재생성 "
                              f"(시도 {attempt + 1})")
            term = {"term": cur.get("ambiguous_term"),
                    "why": cur.get("_term", ""),
                    "dimension": cur.get("coverage_dimension"),
                    "where": cur.get("cited_clauses") or []}
            gkey = f"regen::{cur['case_id']}::{attempt}"
            got = cache.get(gkey, lambda t=term, e=evidence, r=reasons:
                            A.generate_cases(t, e, dims, feedback=r[:4], n=1))
            if not got:
                log("보류", f"{cur['case_id']} 재생성 결과 없음, 폐기")
                return None
            nxt = got[0]
            nxt["case_id"] = cur["case_id"]
            nxt["_term"] = cur.get("_term")
            nxt["_pool_ids"] = [e["id"] for e in candidates]
            cur = nxt
            attempt += 1

    def roundrobin(items):
        """유형·축을 번갈아 처리한다.

        생성 순서대로 처리하면 목표 건수에 먼저 도달하면서 뒤쪽 유형·축이
        통째로 잘린다.
        """
        buckets = {}
        for c in items:
            buckets.setdefault(
                (c.get("case_type"), c.get("coverage_dimension")), []
            ).append(c)
            keys = list(buckets)
        out = []
        while any(buckets[k] for k in keys):
            for k in keys:
                if buckets[k]:
                    out.append(buckets[k].pop(0))
        return out, len(keys)

    ordered, combos = roundrobin(cases)
    log("처리순서", f"유형·축 {combos}개 조합을 번갈아 처리합니다")

    for case in ordered:
        if len(passed) >= C.TARGET_CASES:
            log("중단", f"목표 {C.TARGET_CASES}건 도달, 남은 사례는 처리하지 않음")
            break
        got = process_case(case, "")
        if got:
            passed.append(got)

    # ── STEP 6.5 · FINAL Coverage Check ───────────────────
    # 초기 커버리지는 '후보 풀' 기준이다. 개별 반려를 거치고 나면 살아남은
    # 집합이 다시 편중될 수 있다. 그래서 최종 산출물을 기준으로 다시 본다.
    final_status = "SUFFICIENT"
    for attempt in range(1, C.MAX_FINAL_COVERAGE_ROUNDS + 2):
        fgap, fhave = D.type_gap(passed)
        fdims = [d["name"] for d in dims
                 if not any(c.get("coverage_dimension") == d["name"]
                            for c in passed)]
        log("FINAL Coverage",
            f"{attempt}회차 — 생존 {len(passed)}건 기준 / "
            f"축 {len(dims) - len(fdims)}/{len(dims)} / "
            f"유형 " + ", ".join(f"{t} {fhave.get(t, 0)}/{n}"
                                for t, n in D.CASE_TYPE_TARGET.items()),
            missing_dims=fdims, gap=fgap)

        if not fgap and not fdims:
            log("FINAL Coverage", "충족 — Formatter로 진행합니다")
            break
        if attempt > C.MAX_FINAL_COVERAGE_ROUNDS:
            final_status = "INSUFFICIENT_COVERAGE"
            log("FINAL Coverage",
                "추가 생성 한도 도달 — INSUFFICIENT_COVERAGE 상태로 종료")
            break

        if fdims:
            log("FINAL Coverage", f"  미충족 축: {', '.join(fdims)}")
        if fgap:
            log("FINAL Coverage", "  부족 유형: " +
                ", ".join(f"{t} {n}건" for t, n in fgap.items()))

        # 부족분만 추가 생성한다. 생성된 사례도 같은 경로로
        # Evidence → Case Critic → Code Verify를 다시 거친다.
        existing = [f"[{c.get('coverage_dimension')}/{c.get('case_type')}] "
                    f"{c.get('scenario', '')[:45]}" for c in passed]
        extra = []
        for name in fdims:
            spec = next((d for d in dims if d["name"] == name), None)
            if not spec:
                continue
            pool = [retriever.by_id(a) for a in spec["matched_anchors"]]
            pool = [x for x in pool if x]
            pseudo = {"term": f"{name} 관련 판정", "why": spec["desc"],
                      "dimension": name, "where": spec["matched_anchors"]}
            got = cache.get(
                f"final_dim::{name}::{attempt}",
                lambda p=pseudo, po=pool, ex=existing:
                A.generate_cases(p, po, dims, missing_dimensions=[name],
                                 existing=ex))
            for r in got:
                r["_term"] = pseudo["term"]
                r["_pool_ids"] = [c["id"] for c in pool]
            extra += got
        if fgap:
            pool = retriever.search(
                " ".join(t.get("term", "") for t in terms[:3]))
            pseudo = {"term": "사례 유형 보강", "why": "유형 균형 확보",
                      "dimension": dims[0]["name"],
                      "where": [c["id"] for c in pool[:4]]}
            got = cache.get(
                f"final_types::{attempt}",
                lambda p=pseudo, po=pool, g=dict(fgap), ex=existing:
                A.generate_cases(p, po, dims, missing_types=g,
                                 existing=ex, n=sum(g.values())))
            for r in got:
                r["_term"] = pseudo["term"]
                r["_pool_ids"] = [c["id"] for c in pool]
            extra += got

        if not extra:
            final_status = "INSUFFICIENT_COVERAGE"
            log("FINAL Coverage", "추가 생성 결과 없음 — 보완 불가로 종료")
            break

        log("Generator", f"최종 보완 → {len(extra)}건 생성, "
                         f"Critic·Verify 재통과 시도")
        ordered_extra, _ = roundrobin(extra)
        for case in ordered_extra:
            got = process_case(case, " (보완)")
            if got:
                passed.append(got)

    # ── STEP 7 · Test-case Formatter ──────────────────────
    # 커버리지 미달이면 Formatter로 보내지 않는다.
    # 미달 상태의 목록을 Test Case로 포장해서 내보내면, 받는 쪽에서는
    # 승인된 산출물과 구분할 수 없다. 게이트는 여기에 있어야 한다.
    blocked = (final_status != "SUFFICIENT" and not args.allow_insufficient)
    if blocked:
        log("Test-case Formatter",
            "차단 — INSUFFICIENT_COVERAGE 상태에서는 Test Case를 "
            "생성하지 않습니다")
        log("Test-case Formatter",
            "  부족분을 보완한 뒤 다시 실행하거나, 검토 목적이라면 "
            "--allow-insufficient 로 실행하세요")
    for n, r in enumerate(passed, start=1):
        r["case_id"] = f"EC-{n:03d}"
    # 차단 상태에서는 Formatter 함수를 호출하지 않는다.
    test_cases = [] if blocked else F.format_all(passed)
    # 통계는 Formatter를 거치지 않고 생존 레코드에서 직접 센다.
    summary = survivor_summary(passed, dim_names)
    if not blocked:
        log("Test-case Formatter", f"테스트 케이스 {len(test_cases)}건 생성")
    log("생존 집합",
        "축별 " + ", ".join(f"{k} {v}" for k, v in
                           summary["by_dimension"].items()))
    log("생존 집합",
        "유형별 " + ", ".join(f"{k} {v}" for k, v in
                            summary["by_case_type"].items()))

    # ── STEP 8 · Human Reviewer ───────────────────────────
    # 성능 지표 — 탐지율은 score.py, 여기서는 루프 자체의 품질을 집계한다.
    cite_err = sum(1 for r in rejected
                   if any("인용" in x or "조문 인용" in x
                          for x in (r.get("reject_reasons") or [])))
    total_try = len(passed) + len(rejected)
    log("품질지표",
        f"시도 {total_try}건 중 통과 {len(passed)}건 "
        f"(통과율 {len(passed) / max(total_try, 1) * 100:.0f}%)")
    log("품질지표",
        f"반려 {len(rejected)}건 중 인용 문제 {cite_err}건 "
        f"({cite_err / max(len(rejected), 1) * 100:.0f}%)")

    final_gap, _ = D.type_gap(passed)
    if final_status == "SUFFICIENT":
        log("커버리지", "SUFFICIENT — 최종 생존 집합이 기준을 충족했습니다")
    else:
        log("커버리지", "INSUFFICIENT_COVERAGE — 승인 전 보완이 필요합니다")
        if final_gap:
            log("커버리지", "  부족 유형: " +
                ", ".join(f"{t} {n}건" for t, n in final_gap.items()))

    strip = lambda r: {k: v for k, v in r.items() if not k.startswith("_")}
    (OUT / "edge_cases.json").write_text(
        json.dumps([strip(r) for r in passed], ensure_ascii=False, indent=2),
        encoding="utf-8")
    # 최종 상태를 산출물에 명시한다. INSUFFICIENT_COVERAGE 상태로 나온
    # 결과를 그대로 UAT에 넘기면 안 되기 때문이다.
    (OUT / "test_cases.json").write_text(
        json.dumps({"coverage_status": final_status,
                    "formatter_blocked": blocked,
                    "missing_case_types": final_gap,
                    "test_cases": test_cases},
                   ensure_ascii=False, indent=2), encoding="utf-8")
    (OUT / "rejected.json").write_text(
        json.dumps([strip(r) for r in rejected], ensure_ascii=False, indent=2),
        encoding="utf-8")
    write_markdown(test_cases, OUT / "edge_cases.md")
    write_html(test_cases, rejected, summary, cov, OUT / "demo_output.html",
               coverage_met=(final_status == "SUFFICIENT"),
               final_gap=final_gap, blocked=blocked,
               metrics={"통과율": f"{len(passed) / max(total_try, 1) * 100:.0f}%",
                        "인용 문제 비율":
                        f"{cite_err / max(len(rejected), 1) * 100:.0f}%"})
    cache.save()

    log("Human Reviewer",
        f"상태 {final_status} / 생존 {len(passed)}건 / "
        f"Test Case {len(test_cases)}건 / 반려 {len(rejected)}건 / "
        f"LLM 호출 {cache.calls}회")
    if cache.misses:
        log("캐시", f"미스 {len(cache.misses)}건 — 결과가 불완전할 수 있습니다")
    print(f"        결과: {(OUT / 'demo_output.html').relative_to(ROOT)}")
    if _LOG_FILE:
        _LOG_FILE.close()


def survivor_summary(passed, dimension_names):
    """생존 레코드에서 직접 센다. Formatter를 거치지 않는다."""
    dims = {d: 0 for d in dimension_names}
    types = {}
    for c in passed:
        d = c.get("coverage_dimension")
        if d in dims:
            dims[d] += 1
        t = c.get("case_type", "미확인")
        types[t] = types.get(t, 0) + 1
    return {"by_dimension": dims, "by_case_type": types,
            "total": len(passed)}


def write_markdown(tcs, path):
    lines = ["# 약관 경계사례 테스트 케이스", "",
             "약관 문언만으로 해석이 갈리는 지점입니다. "
             "지급 여부 판정이 아니라 약관 수정 검토용입니다.", ""]
    for t in tcs:
        d = t["dates"]
        lines += [
            f"## {t['case_id']} · {t['ambiguous_term']}",
            f"- 유형: {t['case_type']} / 축: {t['coverage_dimension']} "
            f"/ 상태: {t['status']}",
            f"- 날짜: 계약 {d['contract_date']} / 입원 "
            f"{d['hospitalization_start']}~{d['hospitalization_end']}",
            f"- 계약상태: {t['contract_status']} / 사고원인: {t['cause']} "
            f"/ 급부: {t['benefit_code']}",
            f"- 시나리오: {t['scenario']}",
            f"- 해석 A: {t['interpretation_a']}",
            f"- 해석 B: {t['interpretation_b']}",
            f"- 기대결과: {t['expected_outcome']}",
            f"- 미확정 쟁점: {'; '.join(t['unresolved_issues'])}",
            f"- 검토 질문: {'; '.join(t['review_questions']) or '(없음)'}",
            f"- 근거: {', '.join(e['clause_id'] for e in t['evidence']) or '(없음)'}",
            f"- 권고: {t['recommendation']}",
            "",
        ]
    path.write_text("\n".join(lines), encoding="utf-8")


def esc(s):
    return (str(s if s is not None else "")
            .replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


HTML_HEAD = """<!DOCTYPE html>
<html lang="ko"><head><meta charset="utf-8">
<title>약관 경계사례 테스트 케이스 — 검토 대기</title>
<style>
 body{font-family:'Malgun Gothic',sans-serif;margin:0;background:#f4f5f7;color:#1a1a1a}
 .wrap{max-width:1180px;margin:0 auto;padding:30px 22px 70px}
 h1{font-size:23px;margin:0 0 5px} h2{font-size:18px;margin:34px 0 10px}
 .sub{color:#666;font-size:13.5px;margin-bottom:20px;line-height:1.6}
 .stat{display:flex;gap:9px;margin-bottom:14px;flex-wrap:wrap}
 .stat div{background:#fff;border:1px solid #dcdfe4;border-radius:8px;
  padding:9px 15px;font-size:12.5px}
 .stat b{font-size:19px;display:block;margin-top:2px}
 .card{background:#fff;border:1px solid #dcdfe4;border-radius:10px;
  padding:17px 19px;margin-bottom:13px}
 .hd{display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin-bottom:9px}
 .id{font-weight:700;font-size:15px}
 .term{background:#eef2ff;border-radius:5px;padding:2px 9px;font-size:12.5px}
 .tag{font-size:11px;border-radius:4px;padding:2px 8px;border:1px solid #ccc;
  background:#fafafa}
 .t-경계사례{background:#fff4e5;border-color:#efd3a3}
 .t-판단불가{background:#eceff3;border-color:#c3c9d1}
 .t-반례{background:#f0f7ff;border-color:#b9d4ef}
 .t-정상사례{background:#eef8ef;border-color:#b9dcbd}
 .meta{display:grid;grid-template-columns:repeat(4,1fr);gap:8px;margin:9px 0}
 .meta div{background:#fafbfc;border:1px solid #e9ebee;border-radius:6px;
  padding:7px 10px;font-size:12.5px}
 .meta span{display:block;color:#888;font-size:10.5px;margin-bottom:2px}
 .row{display:grid;grid-template-columns:88px 1fr;gap:9px;padding:6px 0;
  border-top:1px solid #f0f1f3;font-size:13.5px;line-height:1.65}
 .k{color:#777;font-size:11.5px;padding-top:3px}
 .ab{display:grid;grid-template-columns:1fr 1fr;gap:11px;margin:9px 0}
 .ab div{background:#fafbfc;border:1px solid #e6e8eb;border-radius:7px;
  padding:10px 12px;font-size:13px;line-height:1.65}
 .ab b{display:block;margin-bottom:4px;font-size:11.5px;color:#555}
 code{background:#f2f3f5;border-radius:3px;padding:1px 5px;font-size:11.5px}
 .ev{font-size:12px;color:#555;line-height:1.8}
 .ev i{color:#999;font-style:normal;font-size:11px}
 ul{margin:2px 0;padding-left:17px} li{margin:2px 0}
 .chk{background:#fffdf5;border:1px dashed #ddcfa0;border-radius:7px;
  padding:9px 12px;margin-top:9px;font-size:12px;color:#7a6a3a;line-height:1.8}
 .why{color:#b03030;font-size:12.5px;line-height:1.6}
 @media(max-width:860px){.ab,.meta{grid-template-columns:1fr}}
</style></head><body><div class="wrap">"""


def write_html(tcs, rejected, summary, coverage, path,
               coverage_met=True, final_gap=None, metrics=None,
               blocked=False):
    parts = [HTML_HEAD,
             "<h1>약관 경계사례 테스트 케이스 — 검토 대기</h1>",
             '<div class="sub">약관 문언만으로 해석이 갈리는 지점입니다. '
             '지급 여부 판정이 아니라 약관 수정 검토용입니다.<br>'
             '기대결과는 모두 "검토 필요"이며, 판정은 사람이 합니다.</div>']

    parts.append('<div class="stat">'
                 f'<div>검토 대기<b>{len(tcs)}건</b></div>'
                 f'<div>반려<b>{len(rejected)}건</b></div>')
    for k, v in summary["by_case_type"].items():
        parts.append(f'<div>{esc(k)}<b>{v}건</b></div>')
    parts.append("</div>")

    parts.append('<div class="stat">')
    for k, v in summary["by_dimension"].items():
        parts.append(f'<div>{esc(k)}<b>{v}건</b></div>')
    for k, v in (metrics or {}).items():
        parts.append(f'<div>{esc(k)}<b>{esc(v)}</b></div>')
    parts.append("</div>")

    # 커버리지 미달은 숨기지 않고 맨 위에 띄운다.
    # 에이전트가 문제를 알고도 넘어간 것이 아니라, 기록해서 사람에게
    # 넘겼다는 것이 드러나야 한다.
    if not coverage_met:
        gap_txt = ", ".join(f"{t} {n}건 부족"
                            for t, n in (final_gap or {}).items())
        parts.append(
            '<div class="warn"><b>커버리지 미달 상태입니다 — '
            '승인 전 보완이 필요합니다.</b><br>'
            + (esc(gap_txt) + '<br>' if gap_txt else '')
            + ('<b>Test Case 생성이 차단되었습니다.</b> 아래 목록은 '
               '승인된 Test Case가 아니라 검토용 생존 사례입니다.<br>'
               if blocked else '')
            + '추가 생성 한도에 걸려 목표를 채우지 못한 채 종료되었습니다. '
              '정상사례가 없으면 오탐 검증이, 반례가 없으면 유사 사례 '
              '구분 검증이 불가능합니다. 이 목록은 그대로 채택하지 말고 '
              '부족한 유형을 보완한 뒤 UAT로 넘기시기 바랍니다.</div>')
    if coverage.get("reason"):
        parts.append(f'<div class="sub">커버리지 판정: '
                     f'{esc(coverage.get("coverage_status"))} — '
                     f'{esc(coverage["reason"])}</div>')

    for t in tcs:
        d = t["dates"]
        ev = "<br>".join(
            f'<code>{esc(e["clause_id"])}</code> {esc(e["cited_text"])[:90]} '
            f'<i>({esc(e["retrieval_method"])} {e["retrieval_rank"]}위)</i>'
            for e in t["evidence"]) or "(없음)"
        parts.append(f"""<div class="card">
 <div class="hd">
  <span class="id">{esc(t['case_id'])}</span>
  <span class="term">{esc(t['ambiguous_term'])}</span>
  <span class="tag t-{esc(t['case_type'])}">{esc(t['case_type'])}</span>
  <span class="tag">{esc(t['coverage_dimension'])}</span>
  <span class="tag">중요도 {esc(t['severity'])}</span>
  <span class="tag">{esc(t['status'])}</span>
 </div>
 <div class="meta">
  <div><span>계약일</span>{esc(d['contract_date'])}</div>
  <div><span>입원</span>{esc(d['hospitalization_start'])} ~
       {esc(d['hospitalization_end'])}</div>
  <div><span>계약상태 / 사고원인</span>{esc(t['contract_status'])} /
       {esc(t['cause'])}</div>
  <div><span>급부코드</span>{esc(t['benefit_code'])}</div>
 </div>
 <div class="row"><div class="k">시나리오</div><div>{esc(t['scenario'])}</div></div>
 <div class="ab">
  <div><b>해석 A</b>{esc(t['interpretation_a'])}</div>
  <div><b>해석 B</b>{esc(t['interpretation_b'])}</div>
 </div>
 <div class="row"><div class="k">기대결과</div>
   <div><b>{esc(t['expected_outcome'])}</b></div></div>
 <div class="row"><div class="k">미확정 쟁점</div><div><ul>""" +
                     "".join(f"<li>{esc(x)}</li>"
                             for x in t["unresolved_issues"]) + """</ul></div></div>
 <div class="row"><div class="k">검토 질문</div><div><ul>""" +
                     ("".join(f"<li>{esc(x)}</li>"
                              for x in t["review_questions"])
                      or "<li>(없음)</li>") + f"""</ul></div></div>
 <div class="row"><div class="k">근거</div><div class="ev">{ev}</div></div>
 <div class="row"><div class="k">영향</div><div>{esc(t['impact'])}</div></div>
 <div class="row"><div class="k">권고</div><div>{esc(t['recommendation'])}</div></div>
 <div class="chk">검토자 확인 —
  ① 이 경계사례가 실제로 의미 있는가 &nbsp;
  ② 약관 근거가 충분한가 &nbsp;
  ③ 해석 A와 B가 실제로 다른가 &nbsp;
  ④ 약관에 명시해야 할 기준이 있는가 &nbsp;
  ⑤ 테스트 케이스로 채택할 것인가</div>
</div>""")

    if rejected:
        parts.append('<h2>반려된 사례</h2><div class="sub">'
                     '루프가 걸러낸 것들입니다. Code Verify 반려는 '
                     'Case Critic이 통과시킨 것을 코드가 다시 잡은 경우입니다.'
                     '</div>')
        for r in rejected:
            why = (r.get("reject_reasons") or ["사유 미기재"])[0]
            parts.append(f"""<div class="card">
 <div class="hd"><span class="id">{esc(r.get('case_id'))}</span>
 <span class="term">{esc(r.get('ambiguous_term'))}</span>
 <span class="tag">{esc(r.get('rejected_by'))} 반려</span>
 <span class="tag">시도 {esc(r.get('attempt'))}</span></div>
 <div class="why">{esc(why)}</div>
</div>""")

    parts.append("</div></body></html>")
    path.write_text("\n".join(parts), encoding="utf-8")


if __name__ == "__main__":
    main()
