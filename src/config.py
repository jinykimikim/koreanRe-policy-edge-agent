"""설정값 모음.

임베딩 모델을 코드에 박아두지 않는다. 교안이 요구하는 것은
'의미검색 + 키워드검색 하이브리드'이지 특정 모델이 아니다.
환경변수로 바꿀 수 있게 둔다.
"""
import os

# 임베딩 백엔드
#   sentence-transformers : 실제 모델 사용 (기본)
#   fake                  : 인덱스 캐시 로직 단위시험 전용.
#                           실제 하이브리드 검증으로 쓰지 말 것.
EMBED_BACKEND = os.environ.get("EMBED_BACKEND", "sentence-transformers")

# 어떤 모델을 쓸지는 설정으로 뺀다.
EMBED_MODEL = os.environ.get("EMBED_MODEL", "BAAI/bge-m3")

# 검색 파라미터
BM25_TOP_K = int(os.environ.get("BM25_TOP_K", "8"))
DENSE_TOP_K = int(os.environ.get("DENSE_TOP_K", "8"))
RRF_TOP_K = int(os.environ.get("RRF_TOP_K", "8"))
RRF_K = int(os.environ.get("RRF_K", "60"))

# 루프 상한
MAX_TERMS = int(os.environ.get("MAX_TERMS", "5"))
MAX_COVERAGE_ROUNDS = int(os.environ.get("MAX_COVERAGE_ROUNDS", "2"))
MAX_REGENERATE = int(os.environ.get("MAX_REGENERATE", "2"))
# 최종 커버리지 미달 시 추가 생성 batch 횟수. 데모에서는 1회면 충분하다.
MAX_FINAL_COVERAGE_ROUNDS = int(
    os.environ.get("MAX_FINAL_COVERAGE_ROUNDS", "1"))
TARGET_CASES = int(os.environ.get("TARGET_CASES", "10"))

# LLM 호출
CLAUDE_CLI = os.environ.get("CLAUDE_CLI", "claude")
CLAUDE_TIMEOUT = int(os.environ.get("CLAUDE_TIMEOUT", "300"))
