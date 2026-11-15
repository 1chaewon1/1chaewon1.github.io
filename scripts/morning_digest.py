#!/usr/bin/env python3
"""KB 리서치 모닝코멘트 → (Claude 해설) → 비공개 Google Doc 누적 → 카카오톡 알림.

환경변수 (GitHub Actions Secrets):
  ANTHROPIC_API_KEY        Claude API 키
  GOOGLE_SA_JSON           서비스 계정 키(JSON 전체 문자열)
  GOOGLE_DOC_ID            결과를 쌓을 Google Doc ID
  KAKAO_REST_API_KEY       카카오 REST API 키
  KAKAO_REFRESH_TOKEN      카카오 리프레시 토큰
  KAKAO_CLIENT_SECRET      (선택) 카카오 Client Secret 을 켰다면
  GH_PAT, GITHUB_REPOSITORY (선택) 카카오 리프레시 토큰이 갱신되면 Secret 자동 교체

사용법:
  python scripts/morning_digest.py              # 오늘자가 있으면 처리, 이미 처리했으면 종료
  python scripts/morning_digest.py --force      # 오늘자가 아니어도 가장 최근 것 처리
  python scripts/morning_digest.py --list       # 네이버에서 찾은 글 목록만 출력(디버그)
  python scripts/morning_digest.py --dry-run    # Doc/카톡 전송 없이 해설만 출력
"""
from __future__ import annotations

import argparse
import base64
import datetime as dt
import io
import json
import os
import re
import sys
from dataclasses import dataclass
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

KST = dt.timezone(dt.timedelta(hours=9))
UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"}
# 네이버 증권 리서치 목록 페이지들. 모닝코멘트가 올라오는 곳을 모두 훑는다.
LIST_URLS = [
    "https://finance.naver.com/research/market_info_list.naver",
    "https://finance.naver.com/research/invest_list.naver",
    "https://finance.naver.com/research/economy_list.naver",
]
PAGES_PER_LIST = 2
CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "claude-sonnet-5-5")


# ───────────────────────── 1. 네이버에서 수집 ─────────────────────────
@dataclass
class Report:
    title: str
    broker: str
    date: dt.date | None
    pdf_url: str | None
    detail_url: str | None


def _parse_date(text: str) -> dt.date | None:
    m = re.search(r"(\d{2,4})[.\-/](\d{1,2})[.\-/](\d{1,2})", text)
    if not m:
        return None
    y, mo, d = (int(x) for x in m.groups())
    if y < 100:
        y += 2000
    try:
        return dt.date(y, mo, d)
    except ValueError:
        return None


def parse_list(html: str, base_url: str) -> list[Report]:
    soup = BeautifulSoup(html, "html.parser")
    out: list[Report] = []
    for tr in soup.select("table tr"):
        tds = tr.find_all("td")
        if len(tds) < 3:
            continue
        link = next((a for a in tr.find_all("a", href=True) if a.get_text(strip=True)), None)
        if not link:
            continue
        pdf = next(
            (urljoin(base_url, a["href"]) for a in tr.find_all("a", href=True)
             if ".pdf" in a["href"].lower() or "pstatic.net/stock-research" in a["href"]),
            None,
        )
        title = link.get_text(" ", strip=True)
        texts = [td.get_text(" ", strip=True) for td in tds]
        broker = next((t for t in texts[1:] if t and t != title and not _parse_date(t) and not t.isdigit()), "")
        date = next((d for d in (_parse_date(t) for t in texts) if d), None)
        out.append(Report(title, broker, date, pdf, urljoin(base_url, link["href"])))
    return out


def fetch_html(url: str) -> str:
    r = requests.get(url, headers=UA, timeout=30)
    r.raise_for_status()
    r.encoding = "euc-kr"
    return r.text


def collect_reports() -> list[Report]:
    found: list[Report] = []
    for base in LIST_URLS:
        for page in range(1, PAGES_PER_LIST + 1):
            try:
                found += parse_list(fetch_html(f"{base}?page={page}"), base)
            except requests.RequestException as e:
                print(f"[warn] {base} p{page}: {e}", file=sys.stderr)
    return found


def is_kb_morning(r: Report) -> bool:
    return "모닝코멘트" in r.title.replace(" ", "") and ("KB" in r.title.upper() or "KB" in r.broker.upper())


def resolve_pdf(r: Report) -> str:
    if r.pdf_url:
        return r.pdf_url
    if r.detail_url:  # 목록에 PDF 링크가 없으면 상세 페이지에서 찾는다
        soup = BeautifulSoup(fetch_html(r.detail_url), "html.parser")
        for a in soup.find_all("a", href=True):
            if ".pdf" in a["href"].lower() or "pstatic.net/stock-research" in a["href"]:
                return urljoin(r.detail_url, a["href"])
    raise RuntimeError(f"PDF 링크를 찾지 못했습니다: {r.title} ({r.detail_url})")


def pdf_text(url: str) -> str:
    from pypdf import PdfReader
    r = requests.get(url, headers=UA, timeout=60)
    r.raise_for_status()
    reader = PdfReader(io.BytesIO(r.content))
    return "\n".join((p.extract_text() or "") for p in reader.pages).strip()


# ───────────────────────── 2. Claude 해설 ─────────────────────────
PROMPT = """당신은 경제 공부를 막 시작한 사람을 돕는 친절한 경제 과외 선생님입니다.
아래는 오늘 아침 KB증권 리서치 '모닝코멘트' PDF에서 뽑은 텍스트입니다.

독자는 경제 초보자입니다. 용어는 반드시 쉬운 말로 풀고, 숫자는 "그래서 좋은 소식/나쁜 소식 중 무엇인가"까지 말해 주세요.
투자 권유는 하지 말고, 원문에 없는 수치는 지어내지 마세요. 확실하지 않으면 그렇게 말하세요.

다음 구조의 JSON만 출력하세요(코드블록 금지):
{
 "one_line": "오늘 시장을 한 문장으로 (60자 이내)",
 "key_points": ["카톡용 핵심 3줄, 각 45자 이내", "...", "..."],
 "step1": {
   "macro":    [{"fact": "금리·환율·유가·물가·연준·전쟁 관련 오늘의 사실", "explain": "초보자용 풀이 2~3문장"}],
   "earnings": [{"fact": "실적·가이던스·수주 등 기업 소식", "explain": "..."}],
   "theme":    [{"fact": "AI·반도체·전력 등 산업/테마 소식", "explain": "..."}]
 },
 "step2": [{"headline": "원문 속 한 줄 사실", "thought": "속으로 하는 생각: '→ ...?' 형태의 연결 질문/추론", "why": "왜 그렇게 연결되는지 풀이 2~3문장"}],
 "glossary": [{"term": "오늘 나온 어려운 용어", "meaning": "쉬운 설명 + 일상 비유 1개"}],
 "todays_question": "오늘 스스로 더 찾아볼 만한 질문 1개"
}
해당 카테고리 소식이 없으면 빈 배열로 두세요. step2는 4~6개, glossary는 5~10개 권장.

[원문]
"""


def explain(text: str) -> dict:
    import anthropic
    client = anthropic.Anthropic()
    msg = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=6000,
        messages=[{"role": "user", "content": PROMPT + text[:30000]}],
    )
    raw = "".join(b.text for b in msg.content if b.type == "text").strip()
    raw = re.sub(r"^```(?:json)?|```$", "", raw, flags=re.M).strip()
    return json.loads(raw)


def render_doc(d: dict, r: Report, pdf_url: str, date: dt.date) -> tuple[str, list[tuple[int, int, str]]]:
    """Doc에 넣을 본문과 (시작,끝,스타일) 목록을 만든다. 오프셋은 본문 기준 0부터."""
    parts: list[tuple[str, str]] = []  # (text, style)
    def add(text, style="NORMAL_TEXT"): parts.append((text + "\n", style))

    add(f"{date:%Y-%m-%d} ({'월화수목금토일'[date.weekday()]}) {r.title}", "HEADING_1")
    add(d["one_line"])
    add(f"원문: {pdf_url}")
    add("")
    add("STEP 1) 오늘 체크할 것 — 3가지 분류", "HEADING_2")
    for key, label in (("macro", "① 매크로 (금리·환율·유가·물가·연준·전쟁)"),
                       ("earnings", "② 실적·기업 (실적·가이던스·수주)"),
                       ("theme", "③ 산업·테마 (AI·반도체·전력)")):
        add(label, "HEADING_3")
        items = d["step1"].get(key) or []
        if not items:
            add("• 오늘은 특별한 소식 없음")
        for it in items:
            add(f"• {it['fact']}")
            add(f"   → 쉽게 말하면: {it['explain']}")
    add("")
    add("STEP 2) 속으로 생각하기", "HEADING_2")
    for it in d["step2"]:
        add(f"{it['headline']}")
        add(f"→ {it['thought']}")
        add(f"   왜? {it['why']}")
    add("")
    add("용어 노트", "HEADING_2")
    for g in d["glossary"]:
        add(f"• {g['term']}: {g['meaning']}")
    add("")
    add("오늘의 숙제", "HEADING_2")
    add(d["todays_question"])
    add("―" * 20)
    add("")

    text, styles, pos = "", [], 0
    for t, style in parts:
        if style != "NORMAL_TEXT":
            styles.append((pos, pos + len(t), style))
        text += t
        pos += len(t)
    return text, styles


# ───────────────────────── 3. Google Doc ─────────────────────────
def google_session():
    from google.oauth2 import service_account
    from google.auth.transport.requests import AuthorizedSession
    info = json.loads(os.environ["GOOGLE_SA_JSON"])
    creds = service_account.Credentials.from_service_account_info(
        info, scopes=["https://www.googleapis.com/auth/documents"])
    return AuthorizedSession(creds)


def doc_get_text(sess, doc_id: str) -> str:
    r = sess.get(f"https://docs.googleapis.com/v1/documents/{doc_id}")
    r.raise_for_status()
    chunks = []
    for el in r.json().get("body", {}).get("content", []):
        for pe in el.get("paragraph", {}).get("elements", []):
            chunks.append(pe.get("textRun", {}).get("content", ""))
    return "".join(chunks)


def doc_prepend(sess, doc_id: str, text: str, styles: list[tuple[int, int, str]]):
    """가장 위(index 1)에 최신글을 끼워 넣는다."""
    reqs = [{"insertText": {"location": {"index": 1}, "text": text}},
            {"updateParagraphStyle": {"range": {"startIndex": 1, "endIndex": 1 + len(text)},
                                      "paragraphStyle": {"namedStyleType": "NORMAL_TEXT"}, "fields": "namedStyleType"}}]
    for s, e, style in styles:
        reqs.append({"updateParagraphStyle": {"range": {"startIndex": 1 + s, "endIndex": 1 + e},
                                              "paragraphStyle": {"namedStyleType": style}, "fields": "namedStyleType"}})
    r = sess.post(f"https://docs.googleapis.com/v1/documents/{doc_id}:batchUpdate", json={"requests": reqs})
    r.raise_for_status()


# ───────────────────────── 4. 카카오톡 ─────────────────────────
def kakao_access_token() -> str:
    data = {"grant_type": "refresh_token",
            "client_id": os.environ["KAKAO_REST_API_KEY"],
            "refresh_token": os.environ["KAKAO_REFRESH_TOKEN"]}
    if os.environ.get("KAKAO_CLIENT_SECRET"):
        data["client_secret"] = os.environ["KAKAO_CLIENT_SECRET"]
    r = requests.post("https://kauth.kakao.com/oauth/token", data=data, timeout=30)
    if r.status_code != 200:
        raise RuntimeError(f"카카오 토큰 갱신 실패: {r.text} (리프레시 토큰이 만료됐다면 SETUP.md 의 3단계를 다시 하세요)")
    j = r.json()
    if j.get("refresh_token"):  # 만료가 한 달 이내면 새 리프레시 토큰이 내려온다
        update_gh_secret("KAKAO_REFRESH_TOKEN", j["refresh_token"])
    return j["access_token"]


def update_gh_secret(name: str, value: str):
    pat, repo = os.environ.get("GH_PAT"), os.environ.get("GITHUB_REPOSITORY")
    if not (pat and repo):
        print(f"[warn] 새 {name} 이 발급됐지만 GH_PAT 이 없어 저장하지 못했습니다. "
              f"만료 전에 SETUP.md 3단계로 다시 발급하세요.", file=sys.stderr)
        return
    from nacl import encoding, public
    h = {"Authorization": f"Bearer {pat}", "Accept": "application/vnd.github+json"}
    k = requests.get(f"https://api.github.com/repos/{repo}/actions/secrets/public-key", headers=h, timeout=30)
    k.raise_for_status()
    kj = k.json()
    sealed = public.SealedBox(public.PublicKey(kj["key"].encode(), encoding.Base64Encoder())).encrypt(value.encode())
    r = requests.put(f"https://api.github.com/repos/{repo}/actions/secrets/{name}", headers=h, timeout=30,
                     json={"encrypted_value": base64.b64encode(sealed).decode(), "key_id": kj["key_id"]})
    r.raise_for_status()
    print(f"[info] GitHub Secret {name} 갱신 완료")


def kakao_send(token: str, text: str, link: str):
    tpl = {"object_type": "text", "text": text[:200],
           "link": {"web_url": link, "mobile_web_url": link}, "button_title": "상세 해설 보기"}
    r = requests.post("https://kapi.kakao.com/v2/api/talk/memo/default/send",
                      headers={"Authorization": f"Bearer {token}"},
                      data={"template_object": json.dumps(tpl, ensure_ascii=False)}, timeout=30)
    if r.status_code != 200:
        raise RuntimeError(f"카카오 전송 실패: {r.status_code} {r.text}")


def kakao_text(d: dict, date: dt.date) -> str:
    head = f"📈 {date:%m/%d} 모닝코멘트\n{d['one_line']}\n"
    body = "".join(f"• {p}\n" for p in d["key_points"][:3])
    return (head + body).strip()


# ───────────────────────── main ─────────────────────────
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true", help="오늘자가 아니어도 가장 최근 글 처리")
    ap.add_argument("--list", action="store_true", help="수집된 글 목록만 출력")
    ap.add_argument("--dry-run", action="store_true", help="Doc/카톡 전송 없이 결과만 출력")
    ap.add_argument("--pdf-url", help="네이버 수집을 건너뛰고 이 PDF 로 처리(테스트용)")
    a = ap.parse_args()

    today = dt.datetime.now(KST).date()
    if a.pdf_url:
        rep, pdf_url = Report("KB 리서치 모닝코멘트(수동)", "KB증권", today, a.pdf_url, None), a.pdf_url
    else:
        reports = collect_reports()
        if a.list:
            for r in reports:
                print(f"{r.date} | {r.broker} | {r.title} | pdf={'O' if r.pdf_url else 'X'}")
            print(f"총 {len(reports)}건, 그중 KB 모닝코멘트 {sum(map(is_kb_morning, reports))}건")
            return 0
        cands = sorted((r for r in reports if is_kb_morning(r)), key=lambda r: r.date or dt.date.min, reverse=True)
        if not cands:
            print("KB 모닝코멘트를 찾지 못했습니다. `--list` 로 수집 결과를 확인하세요.", file=sys.stderr)
            return 1 if reports == [] else 2
        rep = cands[0]
        if rep.date != today and not a.force:
            print(f"오늘({today}) 자료가 아직 없습니다 (최신: {rep.date}). 휴장일이거나 아직 미게시.")
            return 0
        pdf_url = resolve_pdf(rep)

    sess = None
    if not a.dry_run:
        sess = google_session()
        doc_id = os.environ["GOOGLE_DOC_ID"]
        if pdf_url in doc_get_text(sess, doc_id):
            print("이미 처리한 자료입니다. 종료.")
            return 0

    text = pdf_text(pdf_url)
    if len(text) < 200:
        print("PDF 에서 텍스트를 거의 못 뽑았습니다(이미지 PDF?).", file=sys.stderr)
        return 3
    d = explain(text)
    date = rep.date or today
    body, styles = render_doc(d, rep, pdf_url, date)

    if a.dry_run:
        print(body, "\n--- 카톡 미리보기 ---\n", kakao_text(d, date), sep="")
        return 0

    doc_prepend(sess, doc_id, body, styles)
    kakao_send(kakao_access_token(), kakao_text(d, date), f"https://docs.google.com/document/d/{doc_id}/edit")
    print("완료: Doc 갱신 + 카톡 전송")
    return 0


if __name__ == "__main__":
    sys.exit(main())
