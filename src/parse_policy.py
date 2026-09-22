"""약관 PDF를 관-조-항-호 계층 구조로 파싱한다.

설계 요지
  1. 조각(chunk)은 '항' 또는 '호'까지 잘게 자른다.
  2. 대신 각 조각은 자신의 상위 경로(문서 > 관 > 조 > 항)를 함께 들고 다닌다.
     "제3호"만 떼어놓으면 그것이 지급사유인지 면책사유인지 알 수 없어
     근거로 쓸 수 없기 때문이다.
  3. 별표의 표는 본문 텍스트와 분리해서 표 구조 그대로 추출한다.
     표를 줄글로 읽으면 셀이 뒤섞여 근거가 깨진다.

출력: work/clauses.json
"""
import json
import re
import sys
from pathlib import Path

import pdfplumber

ROOT = Path(__file__).resolve().parents[1]
PDF = ROOT / "data" / "policy_sample.pdf"
OUT = ROOT / "work" / "clauses.json"

HANG = "①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮"
HANG_NUM = {c: i + 1 for i, c in enumerate(HANG)}

NOISE = re.compile(r"^(- \d+ -|무배당 든든입원수술보험 \(2601\.1\)|보험약관|"
                   r"무배당 든든입원수술보험 \(2601\.1\) 보험약관)\s*$")

RE_GWAN = re.compile(r"^(제\d+관)\s+(.+)$")
RE_JO = re.compile(r"^제(\d+)조\s*\((.+?)\)\s*(.*)$")
RE_HO = re.compile(r"^(\d+)\.\s*(.+)$")
RE_TEUKYAK = re.compile(r"^(1-\d+\.\s+.+특별약관)\s*$")
# 별표 '제목줄'만 문서 경계로 인정한다. 본문 안의 [별표2] 참조와 구분하기 위해
# 마침표로 끝나지 않는 짧은 줄만 허용한다.
RE_BYULPYO = re.compile(r"^(\[별표\d+\]\s*[^.]{2,25})$")


def bbox_of(tables):
    return [t.bbox for t in tables]


def outside(boxes):
    def fn(obj):
        for x0, top, x1, bottom in boxes:
            if (obj.get("x0", 0) >= x0 - 1 and obj.get("x1", 0) <= x1 + 1
                    and obj.get("top", 0) >= top - 1
                    and obj.get("bottom", 0) <= bottom + 1):
                return False
        return True
    return fn


def read_pdf(pdf_path):
    """(줄 목록, 표 목록)을 페이지 순서대로 섞어서 돌려준다.

    표는 ('TABLE', rows) 형태로 줄 목록 사이에 끼워 넣는다.
    """
    items = []
    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            tables = page.find_tables()
            boxes = bbox_of(tables)
            src = page.filter(outside(boxes)) if boxes else page
            page_items = []
            for line in src.extract_text_lines():
                ln = line["text"].strip()
                if ln and not NOISE.match(ln):
                    page_items.append((line["top"], "LINE", ln))
            for t in tables:
                rows = [[(c or "").strip().replace("\n", " ")
                         for c in row] for row in t.extract()]
                page_items.append((t.bbox[1], "TABLE", rows))
            # 표가 본문의 어느 위치에 있었는지를 보존해야 표가 어느 별표에
            # 속하는지 알 수 있다. 세로 좌표로 정렬해 원래 순서를 복원한다.
            page_items.sort(key=lambda x: x[0])
            items.extend((kind, val) for _, kind, val in page_items)
    return items


def join_wrapped(items):
    """PDF 줄바꿈으로 끊긴 문장을 다시 붙인다."""
    starters = [RE_GWAN, RE_JO, RE_TEUKYAK, RE_BYULPYO, RE_HO,
                re.compile(r"^[" + HANG + r"]"),
                re.compile(r"^(특별약관|별표|용어풀이)$"),
                re.compile(r"^무배당 든든입원수술보험"),
                re.compile(r"^\[관혈적 처치\]")]
    out = []
    for kind, val in items:
        if kind == "TABLE":
            out.append((kind, val))
            continue
        if (not out or out[-1][0] == "TABLE"
                or any(p.match(val) for p in starters)):
            out.append(("LINE", val))
        else:
            out[-1] = ("LINE", out[-1][1] + " " + val)
    return out


def parse(items):
    chunks = []
    ctx = {"doc": "보통약관", "gwan": None, "jo": None,
           "jo_title": None, "hang": None}
    seq = 0
    seen = set()

    def add(level, no, text, hang=None):
        nonlocal seq
        seq += 1
        parts = [ctx["doc"]]
        if ctx["gwan"]:
            parts.append(ctx["gwan"])
        if ctx["jo"]:
            parts.append(f"제{ctx['jo']}조({ctx['jo_title']})")
        if hang:
            parts.append(f"제{hang}항")
        if level in ("호", "별표호"):
            parts.append(f"제{no}호")

        cid = ctx["doc"]
        if ctx["jo"]:
            cid += f"|제{ctx['jo']}조"
        if hang:
            cid += f"|제{hang}항"
        if level in ("호", "별표호"):
            cid += f"|제{no}호"
        if level == "표행":
            cid += f"|표{no}행"

        # id는 인용 근거로 쓰이므로 반드시 유일해야 한다.
        # 같은 조에 본문 줄이 여러 개인 경우(예: 용어풀이 박스) 꼬리를 붙인다.
        base, n = cid, 2
        while cid in seen:
            cid = f"{base}#{n}"
            n += 1
        seen.add(cid)

        chunks.append({
            "seq": seq, "id": cid, "level": level,
            "doc": ctx["doc"], "gwan": ctx["gwan"],
            "jo": ctx["jo"], "jo_title": ctx["jo_title"],
            "hang": hang, "ho": no if level in ("호", "별표호") else None,
            "path": " > ".join(parts), "text": text,
        })

    for kind, val in items:
        if kind == "TABLE":
            rows = [r for r in val if any(c for c in r)]
            if not rows:
                continue
            header = rows[0]
            for i, row in enumerate(rows[1:], start=1):
                cells = [f"{h}={c}" for h, c in zip(header, row) if c]
                add("표행", i, " / ".join(cells))
            continue

        ln = val

        m = RE_TEUKYAK.match(ln)
        if m:
            ctx.update(doc=m.group(1), gwan=None, jo=None,
                       jo_title=None, hang=None)
            continue

        m = RE_BYULPYO.match(ln)
        if m:
            ctx.update(doc=m.group(1).strip(), gwan=None, jo=None,
                       jo_title=None, hang=None)
            continue

        if ln in ("특별약관", "별표"):
            continue
        if ln.startswith("무배당 든든입원수술보험"):
            ctx.update(doc="보통약관", gwan=None, jo=None,
                       jo_title=None, hang=None)
            continue
        if ln.startswith("(이 약관은 교육") or ln.startswith("상품코드"):
            continue

        m = RE_GWAN.match(ln)
        if m:
            ctx.update(gwan=f"{m.group(1)} {m.group(2)}", jo=None,
                       jo_title=None, hang=None)
            continue

        m = RE_JO.match(ln)
        if m:
            ctx.update(jo=m.group(1), jo_title=m.group(2), hang=None)
            rest = m.group(3).strip()
            if rest:
                add("조문", None, rest)
            continue

        if ln[0] in HANG_NUM:
            ctx["hang"] = HANG_NUM[ln[0]]
            add("항", None, ln[1:].strip(), hang=ctx["hang"])
            continue

        m = RE_HO.match(ln)
        if m:
            level = "별표호" if ctx["doc"].startswith("[별표") else "호"
            add(level, m.group(1), m.group(2).strip(), hang=ctx["hang"])
            continue

        if ln.startswith("용어풀이") or ln.startswith("["):
            add("부속", None, ln)
            continue

        add("조문", None, ln, hang=ctx["hang"])

    return chunks


def main():
    if not PDF.exists():
        sys.exit(f"약관 파일이 없습니다: {PDF}")
    chunks = parse(join_wrapped(read_pdf(PDF)))
    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text(json.dumps(chunks, ensure_ascii=False, indent=2),
                   encoding="utf-8")

    docs = {}
    for c in chunks:
        docs[c["doc"]] = docs.get(c["doc"], 0) + 1
    print(f"[파싱] 조각 {len(chunks)}개 / 문서 {len(docs)}종")
    for d, n in docs.items():
        print(f"        {d}: {n}")
    print(f"[파싱] 저장 {OUT.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
