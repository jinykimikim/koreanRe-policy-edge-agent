"""LLM이 담당하는 컴포넌트.

모든 모델 호출은 _call() 하나를 거친다. run.py는 CLI를 모른다.
_call의 본문만 바꾸면 API나 다른 모델로 옮길 수 있고,
오케스트레이션 코드는 손대지 않는다.

컴포넌트
  find_ambiguous_terms : 약관에서 판정 기준이 없는 표현을 수집
  generate_cases       : Scenario Generator
  coverage_critic      : 사례 '집합'이 판단 경계를 고루 덮었는지 평가
  case_critic          : 사례 '하나'의 의미적 품질을 평가

Coverage Critic과 Case Critic은 보는 대상이 다르다.
Coverage는 집합을, Case는 개별 레코드를 본다.
"""
import json
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import config as C                                        # noqa: E402
import dimensions as D                                    # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / ".claude" / "skills" / "policy-edge-case-probe" / "SKILL.md"


# ── 단일 호출 지점 ────────────────────────────────────────
def _call(system, user, label=""):
    """claude -p 로 1회성 호출을 한다.

    윈도우 기본 인코딩이 cp949라 한글 응답이 깨진다. utf-8을 못박는다.
    """
    if not shutil.which(C.CLAUDE_CLI):
        raise SystemExit(
            "claude CLI를 찾지 못했습니다.\n"
            "  설치 확인: claude --version\n"
            "  로그인 확인: claude -p \"테스트\" --output-format text\n"
            "  경로가 다르면 환경변수 CLAUDE_CLI를 설정해주세요.")

    if label:
        print(f"        ... {label} 호출 중", end="", flush=True)
    t0 = time.time()
    try:
        r = subprocess.run(
            [C.CLAUDE_CLI, "-p", user,
             "--append-system-prompt", system, "--output-format", "text"],
            capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=C.CLAUDE_TIMEOUT)
    except subprocess.TimeoutExpired:
        print()
        raise SystemExit(f"CLI 응답이 {C.CLAUDE_TIMEOUT}초를 넘었습니다.")
    if label:
        print(f" ({time.time() - t0:.0f}초)")
    if r.returncode != 0:
        raise SystemExit(f"CLI 호출 실패(코드 {r.returncode})\n"
                         f"{(r.stderr or '')[:500]}")
    return r.stdout or ""


def _json_blocks(text):
    """응답에서 JSON 객체만 건져낸다. 앞뒤 설명이 섞여도 동작한다."""
    if not text:
        return []
    text = re.sub(r"```(?:json)?", "", text)
    out, depth, start = [], 0, None
    for i, ch in enumerate(text):
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}" and depth:
            depth -= 1
            if depth == 0:
                try:
                    out.append(json.loads(text[start:i + 1]))
                except json.JSONDecodeError:
                    pass
    return out


def _render(clauses):
    return "\n".join(f"[{c['id']}] ({c['path']})\n{c['text']}"
                     for c in clauses)


def _skill():
    return SKILL.read_text(encoding="utf-8")


# ── STEP 1 · 모호 표현 탐지 ───────────────────────────────
def find_ambiguous_terms(clauses, dims):
    user = (
        "아래는 약관 전체 조문이다.\n\n" + _render(clauses) +
        "\n\n판정에 쓰이는데 정의되지 않은 표현, 그리고 문서 간에 서로 다르게 "
        "정해진 지점을 찾아라.\n\n"
        "아래 판단 경계 축을 고루 훑어라. 한 축에 몰리지 않게 하고, "
        "가능하면 축마다 최소 1건씩 뽑아라.\n" + D.describe(dims) +
        "\n\n각 항목을 다음 JSON 객체로 출력한다.\n"
        '{"term": "표현 그대로", "why": "왜 문제인지 한 문장", '
        '"dimension": "위 축 이름 중 하나", "where": ["조각 id"]}\n'
        "다른 설명은 쓰지 마라.")
    return _json_blocks(_call(_skill(), user, "모호 표현 탐지"))


# ── STEP 2 · Scenario Generator ───────────────────────────
def generate_cases(term, clauses, dims, feedback=None,
                   missing_dimensions=None, missing_types=None,
                   existing=None, n=2):
    """판단 경계가 존재하는 시나리오를 만든다.

    feedback           : Case Critic / Verify 반려 사유 (재생성)
    missing_dimensions : Coverage Critic이 지적한 미충족 축 (추가 생성)
    missing_types      : 부족한 사례 유형과 건수 {유형: 건수}
                         축만 전달하면 Generator가 계속 경계사례만 만든다.
                         유형을 명시해야 정상사례와 반례가 생긴다.
    existing           : 이미 만든 사례 요약. 중복을 피하게 한다.
    """
    allowed = [c["id"] for c in clauses]
    user = (
        f"검토 대상 표현: {term.get('term')}\n"
        f"문제 요지: {term.get('why')}\n"
        f"판단 경계 축: {term.get('dimension', '미지정')}\n\n"
        "관련 조문:\n" + _render(clauses) +
        "\n\n[인용 가능한 조각 id — 이 목록에 있는 것만 쓸 수 있다]\n"
        + "\n".join(f"- {a}" for a in allowed) +
        "\n이 목록에 없는 id를 cited_clauses에 넣으면 그 레코드는 "
        "폐기된다. 목록 밖 조문이 필요하면 만들지 말고 "
        "cited_clauses를 비우고 status를 판단보류로 한다.\n" +
        f"\n\n이 표현 때문에 해석이 갈리는 사례를 최대 {n}건 만들어라.\n"
        "약관 문장을 바꿔 쓴 것이 아니라, 실제로 판단이 갈리는 경계가 "
        "존재하는 상황이어야 한다.\n"
        "SKILL.md의 테스트 케이스 양식에 맞춘 JSON 객체만 출력한다.")

    if existing:
        user += ("\n\n[이미 만든 사례 — 중복을 피하라]\n" +
                 "\n".join(f"- {e}" for e in existing))
    if missing_dimensions:
        user += ("\n\n[아래 축이 아직 비어 있다. 이 축을 채우는 사례만 만들어라]\n"
                 + "\n".join(f"- {m}" for m in missing_dimensions))
    if missing_types:
        user += ("\n\n[아래 사례 유형이 부족하다. 지정한 건수만큼 "
                 "그 유형으로 만들어라. case_type 값을 정확히 그 유형으로 "
                 "지정한다]\n" + D.describe_types(missing_types) +
                 "\n\n특히 '정상사례'는 문제를 만들어내지 말고, 약관이 "
                 "명확하게 답하는 상황을 그대로 쓴다. 해석 A와 B는 제시하되 "
                 "어느 쪽이 왜 우세한지 밝힌다. 이런 사례가 있어야 "
                 "에이전트가 모든 건을 문제라고 하지 않는지 검증할 수 있다.")
    if feedback:
        user += ("\n\n[이전 시도가 반려되었다. 아래를 반영해 다시 작성하라]\n"
                 + "\n".join(f"- {f}" for f in feedback))

    label = "사례 생성" if not feedback else "사례 재생성"
    return _json_blocks(_call(_skill(), user,
                              f"{label}({str(term.get('term'))[:12]})"))


# ── STEP 3 · Coverage Critic ──────────────────────────────
COVERAGE_SYSTEM = """너는 생성된 사례 '집합'의 커버리지를 평가한다.

개별 사례가 타당한지는 보지 않는다. 그것은 다른 검토자의 일이다.
너는 집합 전체가 중요한 판단 경계를 고루 덮었는지만 본다.

두 가지 축으로 본다.
1. 판단 경계 축(dimension): 주어진 축 중 어느 것이 비어 있는가
2. 사례 유형 균형: 정상사례, 경계사례, 반례, 판단불가가 목표치를 채웠는가
   - 목표치와 현재 건수를 함께 준다. 부족한 유형과 건수를
     missing_types에 정확히 적는다.
   - 전부 경계사례이면 불충분하다. 정상사례가 없으면 오탐을 걸러낼 수
     없고, 반례가 없으면 비슷한 건을 구분하는지 확인할 수 없다.
   - 약관만으로 결론이 안 나는 '판단불가' 사례가 하나도 없으면
     그 자체가 누락이다.

같은 축에서 사실상 같은 상황이 반복되면 중복으로 본다.

다음 JSON 객체 하나만 출력한다. 다른 설명은 쓰지 마라.
{
  "coverage_status": "sufficient" 또는 "insufficient",
  "covered_dimensions": ["..."],
  "missing_dimensions": ["..."],
  "missing_types": {"유형": 부족건수},
  "duplicate_dimensions": ["..."],
  "case_type_balance": {"정상사례": 0, "경계사례": 0, "반례": 0, "판단불가": 0},
  "reason": "판단 근거 한두 문장",
  "additional_scenarios_required": ["어떤 사례를 더 만들어야 하는지"]
}"""


def coverage_critic(cases, dims, type_gap=None, have=None):
    summary = [{
        "case_id": c.get("case_id"),
        "case_type": c.get("case_type"),
        "coverage_dimension": c.get("coverage_dimension"),
        "ambiguous_term": c.get("ambiguous_term"),
        "scenario": c.get("scenario"),
    } for c in cases]
    targets = ", ".join(f"{t} {n}건 이상"
                        for t, n in D.CASE_TYPE_TARGET.items())
    user = ("평가 대상 판단 경계 축:\n" + D.describe(dims) +
            f"\n\n사례 유형 목표치: {targets}\n"
            f"현재 유형별 건수: {json.dumps(have or {}, ensure_ascii=False)}\n"
            f"부족한 유형: {json.dumps(type_gap or {}, ensure_ascii=False)}"
            "\n\n생성된 사례 집합:\n" +
            json.dumps(summary, ensure_ascii=False, indent=2))
    blocks = _json_blocks(_call(COVERAGE_SYSTEM, user, "커버리지 평가"))
    return blocks[0] if blocks else {}


# ── STEP 5 · Case Critic ──────────────────────────────────
CASE_CRITIC_SYSTEM = """너는 개별 사례의 품질을 심사한다.

작성 과정은 보지 않는다. 결과물과 근거 조문만 본다.
집합 전체의 커버리지는 네 일이 아니다. 사례 하나하나만 본다.

다음을 각각 확인한다.
1. 해석A와 해석B가 실제로 서로 다른 결론인가. 같은 말을 두 번 쓴 것은 반려.
2. 시나리오가 구체적인가. 날짜·일수 없이 추상적이면 반려.
3. 인용한 원문이 주어진 조문에 실제로 있는 문장인가.
4. 근거 조문이 그 해석을 실제로 뒷받침하는가.
   조문은 존재하지만 결론과 무관하면 반려.
5. 지급 여부에 대한 최종 결론을 내리고 있지 않은가. 내렸으면 반려.
6. 날짜·일수 계산이 맞는가. 기간을 다투는 사안에서 숫자가 틀리면 반려.

각 레코드에 대해 다음 JSON 객체를 출력한다. 다른 설명은 쓰지 마라.
{"case_id": "...", "verdict": "통과" 또는 "반려", "reasons": ["반려 사유"]}"""


def case_critic(cases, evidence):
    user = ("참조 조문 (이 목록이 인용 가능한 전부다):\n" + _render(evidence) +
            "\n\n검토 대상 레코드:\n" +
            json.dumps(cases, ensure_ascii=False, indent=2))
    return _json_blocks(_call(CASE_CRITIC_SYSTEM, user, "사례 비평"))


# ── STEP 4 보조 · 검색 질의 추출 (코드) ────────────────────
def evidence_queries(case):
    """시나리오에서 판단 쟁점을 뽑아 검색 질의를 만든다.

    LLM을 다시 부르지 않고 레코드의 구조화된 필드에서 조립한다.
    호출을 아끼는 목적도 있지만, 질의가 매번 같아야 재검색 결과가
    재현되기 때문이기도 하다.
    """
    parts = [case.get("ambiguous_term", "")]
    parts += case.get("unresolved_issues") or []
    for k in ("benefit_code", "cause", "contract_status"):
        v = case.get(k)
        if v and v != "미확인":
            parts.append(str(v))
    q = " ".join(p for p in parts if p).strip()
    return q or case.get("scenario", "")[:60]
