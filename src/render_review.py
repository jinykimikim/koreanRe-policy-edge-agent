"""저장된 실행 결과로 outputs/edge_cases.html만 다시 만든다.

Agent를 다시 돌리지 않는다. 모델 호출, 검색, 캐시(work/cache.json)를
건드리지 않고 run.py가 마지막 실행에서 남긴 파일만 읽는다.

  outputs/edge_cases.json  : 생존 레코드(passed)
  outputs/rejected.json    : 반려 레코드
  outputs/test_cases.json  : Coverage 상태와 Formatter Gate 결과

    python src/render_review.py
"""
import json
import sys

import run as R

EDGE = R.OUT / "edge_cases.json"
REJECTED = R.OUT / "rejected.json"
TEST_CASES = R.OUT / "test_cases.json"


def main():
    for p in (EDGE, REJECTED, TEST_CASES):
        if not p.exists():
            sys.exit(f"{p.relative_to(R.ROOT)}이 없습니다. "
                     "먼저 python src/run.py 를 실행해주세요.")
    passed = json.loads(EDGE.read_text(encoding="utf-8"))
    rejected = json.loads(REJECTED.read_text(encoding="utf-8"))
    gate = json.loads(TEST_CASES.read_text(encoding="utf-8"))

    path = R.OUT / "edge_cases.html"
    R.write_review_html(passed, rejected, path,
                        coverage_status=gate["coverage_status"],
                        blocked=gate["formatter_blocked"])
    print(f"검토 후보 {len(passed)}건 / 반려 {len(rejected)}건 / "
          f"Coverage {gate['coverage_status']} → "
          f"{path.relative_to(R.ROOT)}")


if __name__ == "__main__":
    main()
