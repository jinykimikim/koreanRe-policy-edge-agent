"""코드로 확인 가능한 것은 모델에게 묻지 않는다.

Case Critic도 같은 모델이라 같은 착각을 한다. 조문이 실제로 존재하는지,
인용문이 원문과 글자 그대로 일치하는지, 날짜 형식이 맞는지는
찾아보거나 계산하면 되는 일이므로 여기서 기계적으로 다시 본다.

Case Critic이 통과시킨 레코드를 여기서 반려하는 경우가 나오면,
그것이 '두 에이전트가 동의했다고 맞는 것은 아니다'의 실물 증거다.

Case Critic과 역할이 일부 겹친다(인용 실재, 해석 A/B 동일 여부).
의도된 중복이다. 모델이 놓쳐도 코드가 잡는 이중 구조를 만든 것이고,
겹치는 항목은 둘 다 '확인 가능한 사실'에 속해 코드가 더 정확하다.
"""
import re

REQUIRED = ["case_id", "case_type", "coverage_dimension", "ambiguous_term",
            "scenario", "interpretation_a", "interpretation_b",
            "expected_outcome", "cited_clauses", "recommendation", "status"]

STRUCTURED = ["dates", "contract_status", "cause", "benefit_code"]

# 지급 여부를 확정하는 표현. expected_outcome 값과 서술 양쪽을 본다.
CONCLUSIVE = re.compile(
    r"(지급해야 한다|지급하여야 한다|지급된다|지급 대상이다|지급함이 타당|"
    r"부지급이다|지급하지 않는다|면책이다|보상하지 않는다|지급 확정)")

OUTCOME_BAD = re.compile(r"^(지급|부지급|면책|보상|미지급)")
DATE = re.compile(r"^(\d{4}-\d{2}-\d{2}|미확인)$")

VALID_TYPE = {"정상사례", "경계사례", "반례", "판단불가"}
VALID_STATUS = {"검토요청", "판단보류"}


def normalize(s):
    return re.sub(r"\s+", "", s or "")


def verify(record, clause_index, dimension_names):
    """반려 사유 목록을 돌려준다. 비어 있으면 통과."""
    problems = []

    for k in REQUIRED:
        if not record.get(k):
            problems.append(f"필수 항목 누락: {k}")
    for k in STRUCTURED:
        if k not in record:
            problems.append(f"구조화 항목 누락: {k}")

    ctype = record.get("case_type")
    if ctype and ctype not in VALID_TYPE:
        problems.append(f"case_type 값 오류: {ctype}")
    status = record.get("status", "")
    if status and status not in VALID_STATUS:
        problems.append(f"status 값 오류: {status}")

    dim = record.get("coverage_dimension")
    if dim and dimension_names and dim not in dimension_names:
        problems.append(f"정의되지 않은 coverage_dimension: {dim}")

    # 기대결과는 판정이 아니어야 한다
    outcome = str(record.get("expected_outcome", ""))
    if OUTCOME_BAD.match(outcome.strip()):
        problems.append(f"expected_outcome이 지급 판정임: {outcome}")

    # 인용 검증
    cited = record.get("cited_clauses") or []
    for cid in cited:
        if cid not in clause_index:
            problems.append(f"존재하지 않는 조문 인용: {cid}")
    # cited_text는 코드가 원문에서 주입하므로 정상이라면 항상 일치한다.
    # 안전망으로 남겨둔다. 여기서 걸리면 주입 단계가 실패한 것이다.
    for cid, quoted in zip(cited, record.get("cited_text") or []):
        src = clause_index.get(cid)
        if src and normalize(quoted) not in normalize(src["text"]):
            problems.append(f"인용문 주입 실패 — 원문과 불일치: {cid}")
    if not cited and status != "판단보류":
        problems.append("근거 없는 레코드는 status가 판단보류여야 함")

    # 해석 두 갈래
    a = normalize(record.get("interpretation_a", ""))
    b = normalize(record.get("interpretation_b", ""))
    if a and b:
        if a == b:
            problems.append("해석A와 해석B가 동일함")
        elif len(a) < 15 or len(b) < 15:
            problems.append("해석 서술이 지나치게 짧음")

    # 지급 결론 금지
    for field in ("interpretation_a", "interpretation_b", "recommendation"):
        if CONCLUSIVE.search(str(record.get(field, ""))):
            problems.append(f"{field}에 지급 여부 결론이 포함됨")

    # 날짜 형식과 순서
    dates = record.get("dates") or {}
    for k, v in dates.items():
        if not DATE.match(str(v)):
            problems.append(f"날짜 형식 오류: {k}={v}")
    s, e = dates.get("hospitalization_start"), dates.get("hospitalization_end")
    if DATE.match(str(s) or "") and DATE.match(str(e) or ""):
        if "미확인" not in (s, e) and s > e:
            problems.append(f"입원일이 퇴원일보다 늦음: {s} > {e}")

    # 판단불가 사례의 일관성
    if ctype == "판단불가" and status != "판단보류":
        problems.append("case_type이 판단불가인데 status가 판단보류가 아님")

    return problems


def verify_all(records, clauses, dimension_names=None):
    index = {c["id"]: c for c in clauses}
    passed, rejected = [], []
    for r in records:
        problems = verify(r, index, dimension_names or [])
        if problems:
            rejected.append(dict(r, verify_problems=problems))
        else:
            passed.append(r)
    return passed, rejected
