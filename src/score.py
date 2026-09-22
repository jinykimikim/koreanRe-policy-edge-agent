"""채점: 심어둔 함정 중 몇 개를 덮었는지 대조한다.

가상 약관에는 결함 12건을 의도적으로 심어두었다. 에이전트가 찾아낸 레코드의
인용 조문과 대조해서 자동으로 채점한다.

"잘 찾네요"보다 "12개 중 N개를 찾았고, 못 찾은 것은 이런 성격입니다"가
훨씬 설득력이 있다. 못 찾은 것의 성격이 오히려 설계 한계를 말해준다.

사용법: python src/score.py
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RESULT = ROOT / "outputs" / "edge_cases.json"

# 함정별 판정 기준
#   clauses: 이 조문 중 하나라도 인용했으면 접근한 것으로 본다
#   keywords: 표현이나 시나리오에 이 단어가 있으면 탐지로 본다
TRAPS = [
    {"id": "T1", "name": "'직접적인 치료' 미정의", "axis": "정의누락",
     "clauses": ["보통약관|제2조|제2항|제3호", "보통약관|제2조|제2항|제5호"],
     "keywords": ["직접적인 치료"]},
    {"id": "T2", "name": "'주된 목적' 판단기준 부재 (요양병원 충돌)",
     "axis": "조항충돌",
     "clauses": ["보통약관|제5조|제5호",
                 "1-1. 질병입원일당 특별약관|제4조|제6항"],
     "keywords": ["주된 목적", "요양"]},
    {"id": "T3", "name": "입원일수 산정 경계 (퇴원일 산입)", "axis": "산정기준",
     "clauses": ["[별표1] 입원일수 산정기준표|제1호"],
     "keywords": ["퇴원한 날", "입원일수는"]},
    {"id": "T4", "name": "'외박' 미정의", "axis": "정의누락",
     "clauses": ["[별표1] 입원일수 산정기준표|제3호"],
     "keywords": ["외박"]},
    {"id": "T5", "name": "재입원 처리 충돌 (특약 대 별표1)", "axis": "문서간충돌",
     "clauses": ["1-1. 질병입원일당 특별약관|제4조|제3항",
                 "[별표1] 입원일수 산정기준표|제4호"],
     "keywords": ["재입원", "동일한 원인", "동일한 질병"]},
    {"id": "T6", "name": "관혈적 수술 정의 대 별표2", "axis": "문서간충돌",
     "clauses": ["보통약관|제2조|제2항|제5호",
                 "[별표2] 수술분류표|표4행", "[별표2] 수술분류표|표5행",
                 "[별표2] 수술분류표|표6행"],
     "keywords": ["관혈적", "내시경", "체외충격파", "경피적"]},
    {"id": "T7", "name": "중환자실 중복지급 충돌", "axis": "문서간충돌",
     "clauses": ["1-2. 상해입원일당 특별약관|제3조|제3항",
                 "1-3. 중환자실 입원일당 특별약관|제3조|제2항",
                 "[별표3] 중복지급 기준표|표2행"],
     "keywords": ["중환자실", "중복지급"]},
    {"id": "T8", "name": "감액 종료일 '1년이 되는 날'", "axis": "기간경계",
     "clauses": ["1-1. 질병입원일당 특별약관|제4조|제5항"],
     "keywords": ["1년이 되는 날", "감액"]},
    {"id": "T9", "name": "보장개시일 기준 불일치", "axis": "기간경계",
     "clauses": ["보통약관|제13조|제1항",
                 "1-1. 질병입원일당 특별약관|제4조|제4항",
                 "1-2. 상해입원일당 특별약관|제3조|제2항"],
     "keywords": ["보장개시일", "90일", "계약일부터"]},
    {"id": "T10", "name": "갱신 시점 계속 중인 입원", "axis": "규정공백",
     "clauses": ["보통약관|제12조|제4항"],
     "keywords": ["갱신일 현재", "계속 중인 입원"]},
    {"id": "T11", "name": "입원 중 타 병원 통원", "axis": "규정공백",
     "clauses": ["1-4. 통원일당 특별약관|제2조|제3항",
                 "[별표3] 중복지급 기준표|표4행"],
     "keywords": ["입원 중", "통원이 중복", "타 병원"]},
    {"id": "T12", "name": "제3자 전문의 의견 (판단 불가)", "axis": "판단불가",
     "clauses": ["보통약관|제4조|제3항"],
     "keywords": ["제3자", "전문의", "의료자문"]},
]


def blob(rec):
    parts = [rec.get("ambiguous_term", ""), rec.get("scenario", ""),
             rec.get("interpretation_a", ""), rec.get("interpretation_b", ""),
             rec.get("recommendation", ""), rec.get("issue_type", "")]
    return " ".join(parts)


def match(trap, records):
    """(탐지 여부, 근거가 된 case_id 목록)"""
    hits = []
    for r in records:
        cited = set(r.get("cited_clauses") or [])
        by_clause = bool(cited & set(trap["clauses"]))
        text = blob(r)
        by_kw = any(k in text for k in trap["keywords"])
        if by_clause and by_kw:
            hits.append(r.get("case_id"))
    return bool(hits), hits


def main():
    if not RESULT.exists():
        sys.exit("먼저 python src/run.py 를 실행해주세요.")
    records = json.loads(RESULT.read_text(encoding="utf-8"))

    found, missed = [], []
    for trap in TRAPS:
        ok, hits = match(trap, records)
        (found if ok else missed).append((trap, hits))

    print(f"\n[채점] 심어둔 결함 {len(TRAPS)}건 중 "
          f"{len(found)}건 재현 / {len(missed)}건 미재현")
    print(f"        (생성된 레코드 {len(records)}건 기준)")
    print("        ※ 이 수치는 '심어둔 결함 재현율'이며 정확도나 recall이")
    print("           아닙니다. 정답지를 만든 사람과 스킬을 쓴 사람이 같고,")
    print("           오탐률(문제없는 조항을 문제라고 한 비율)은 측정하지")
    print("           않았습니다. 실제 성능은 이미 발생한 민원·분쟁 건을")
    print("           정답지로 삼아야 나옵니다.\n")

    print("  ■ 재현")
    for trap, hits in found:
        print(f"    {trap['id']} [{trap['axis']}] {trap['name']}")
        print(f"        → {', '.join(hits)}")

    print("\n  □ 미재현")
    for trap, _ in missed:
        print(f"    {trap['id']} [{trap['axis']}] {trap['name']}")

    axes = {}
    for trap, _ in found:
        axes.setdefault(trap["axis"], [0, 0])[0] += 1
    for trap in TRAPS:
        axes.setdefault(trap["axis"], [0, 0])[1] += 1

    print("\n  축별 재현율")
    for axis, (hit, total) in axes.items():
        bar = "■" * hit + "□" * (total - hit)
        print(f"    {axis:<8} {bar}  {hit}/{total}")

    extra = [r.get("case_id") for r in records
             if not any(match(t, [r])[0] for t in TRAPS)]
    if extra:
        print(f"\n  ＋ 함정목록에 없던 지점: {len(extra)}건 "
              f"({', '.join(extra)})")
        print("     의도하지 않은 결함을 찾아낸 경우입니다.")
    print()


if __name__ == "__main__":
    main()
