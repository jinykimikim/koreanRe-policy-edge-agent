"""Test-case Formatter.

검토용 edge case 레코드를, 업무로 넘길 수 있는 테스트 케이스 스키마로
변환한다. LLM을 부르지 않는 순수 변환 단계다.

왜 나누는가
  edge case 레코드는 사람이 읽는 검토 의견에 가깝다.
  테스트 케이스는 시스템이 받아가는 구조다. 날짜·계약상태·사고원인·
  급부코드가 문장 안에 묻혀 있으면 UAT나 지급심사 검토로 넘길 때
  사람이 다시 뜯어내야 한다.

기대결과는 절대 지급/부지급으로 확정하지 않는다.
약관만으로 결론이 안 나는 사례는 그 사실 자체를 유형으로 보존한다.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

UNKNOWN = "미확인"
DATE_KEYS = ["contract_date", "accident_date",
             "hospitalization_start", "hospitalization_end"]


def _dates(rec):
    src = rec.get("dates") or {}
    return {k: (src.get(k) or UNKNOWN) for k in DATE_KEYS}


def _evidence(rec):
    """근거를 검증 가능한 형태로 편다.

    retrieval 메타데이터(어떤 검색기가 몇 위로 올렸는지)를 함께 남겨
    나중에 검색 품질을 되짚을 수 있게 한다.
    """
    out = []
    cites = rec.get("cited_clauses") or []
    texts = rec.get("cited_text") or []
    meta = {e["id"]: e for e in (rec.get("_evidence") or [])}
    for i, cid in enumerate(cites):
        e = meta.get(cid, {})
        rt = e.get("retrieval") or {}
        out.append({
            "clause_id": cid,
            "document": e.get("doc", UNKNOWN),
            "path": e.get("path", UNKNOWN),
            "cited_text": texts[i] if i < len(texts) else UNKNOWN,
            "retrieval_method": rt.get("method", "미기록"),
            "retrieval_rank": rt.get("rank"),
            "rrf_score": rt.get("rrf_score"),
        })
    return out


def to_test_case(rec, seq):
    tc = {
        "case_id": f"TC-{seq:03d}",
        "source_case_id": rec.get("case_id"),
        "case_type": rec.get("case_type", UNKNOWN),
        "coverage_dimension": rec.get("coverage_dimension", UNKNOWN),
        "ambiguous_term": rec.get("ambiguous_term", UNKNOWN),

        "dates": _dates(rec),
        "contract_status": rec.get("contract_status") or UNKNOWN,
        "cause": rec.get("cause") or UNKNOWN,
        "benefit_code": rec.get("benefit_code") or UNKNOWN,

        "scenario": rec.get("scenario", ""),
        "interpretation_a": rec.get("interpretation_a", ""),
        "interpretation_b": rec.get("interpretation_b", ""),

        # 지급 결론을 내지 않는다. 사람이 판단할 쟁점만 남긴다.
        "expected_outcome": "검토 필요",
        "unresolved_issues": rec.get("unresolved_issues") or [],
        "review_questions": rec.get("review_questions") or [],

        "evidence": _evidence(rec),
        "impact": rec.get("impact", UNKNOWN),
        "severity": rec.get("severity", UNKNOWN),
        "confidence": rec.get("confidence", UNKNOWN),
        "recommendation": rec.get("recommendation", ""),
        "status": rec.get("status", "검토요청"),
        "review_state": "미검토",          # Human Reviewer가 채울 칸
    }

    # 미확정 쟁점이 비어 있으면 최소한 표현 자체를 남긴다.
    if not tc["unresolved_issues"]:
        tc["unresolved_issues"] = [f"'{tc['ambiguous_term']}'의 판정 기준"]
    return tc


def format_all(records):
    return [to_test_case(r, i) for i, r in enumerate(records, start=1)]


def coverage_summary(test_cases, dimension_names):
    dims = {d: 0 for d in dimension_names}
    types = {}
    for tc in test_cases:
        d = tc.get("coverage_dimension")
        if d in dims:
            dims[d] += 1
        t = tc.get("case_type", UNKNOWN)
        types[t] = types.get(t, 0) + 1
    return {"by_dimension": dims, "by_case_type": types,
            "total": len(test_cases)}
