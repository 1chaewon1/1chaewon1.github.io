#!/usr/bin/env python3
"""KB 리서치 모닝코멘트 → Claude 초보자용 해설 → Gmail 로 발송.

환경변수 (GitHub Actions Secrets):
  ANTHROPIC_API_KEY    Claude API 키
  GMAIL_ADDRESS        보내는 Gmail 주소
  GMAIL_APP_PASSWORD   Gmail 앱 비밀번호(16자리)
  MAIL_TO              (선택) 받는 주소. 없으면 GMAIL_ADDRESS 로 나에게 보냄

사용법:
  python scripts/morning_digest.py --wait 50  # 오늘자가 올라올 때까지 최대 50분 기다렸다가 발송
  python scripts/morning_digest.py --force    # 오늘자가 아니어도 가장 최근 것 발송
  python scripts/morning_digest.py --list     # 네이버에서 찾은 글 목록만 출력(디버그)
  python scripts/morning_digest.py --dry-run  # 메일 발송 없이 결과만 출력
"""
from __future__ import annotations

import argparse
import datetime as dt
import io
import html as H
import smtplib
import time
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
import json
import os
import re
import sys
from dataclasses import dataclass

import requests

KST = dt.timezone(dt.timedelta(hours=9))
UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"}
# 네이버 증권 리서치 목록 페이지들. 모닝코멘트가 올라오는 곳을 모두 훑는다.
CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "claude-sonnet-5-5")


# ───────────────────────── 1. 네이버에서 수집 ─────────────────────────
# 네이버 증권(Npay 증권)의 리서치 화면이 쓰는 JSON API. HTML 파싱이 필요 없다.
API = "https://stock.naver.com/api/stockSecurity/researches/v2/market"
HEADERS = {**UA, "Accept": "application/json", "Referer": "https://stock.naver.com/research/daily"}


@dataclass
class Report:
    title: str
    broker: str
    date: dt.date | None
    pdf_url: str | None
    content: str  # 목록 API가 주는 요약 본문(텍스트)
    nid: str = ""


def html_to_text(h: str) -> str:
    h = re.sub(r"<br\s*/?>|</p>", "\n", h)
    return H.unescape(re.sub(r"<[^>]+>", "", h)).replace("\xa0", " ").strip()


def collect_reports(size: int = 40) -> list[Report]:
    r = requests.get(API, params={"index": 0, "size": size}, headers=HEADERS, timeout=30)
    r.raise_for_status()
    out = []
    for it in r.json().get("items", []):
        out.append(Report(it["title"], it.get("brokerName", ""), dt.date.fromisoformat(it["writeDate"]),
                          None, html_to_text(it.get("content", "")), str(it["nid"])))
    return out


def is_kb_morning(r: Report) -> bool:
    return "모닝코멘트" in r.title.replace(" ", "") and ("KB" in r.title.upper() or "KB" in r.broker.upper())


def resolve_pdf(r: Report) -> str:
    """상세 API 에서 원문 PDF 주소를 가져온다."""
    d = requests.get(f"{API}/{r.nid}", headers=HEADERS, timeout=30)
    d.raise_for_status()
    url = d.json().get("attachUrl")
    if not url:
        raise RuntimeError(f"PDF 링크가 없습니다: {r.title}")
    return url


def pdf_text(url: str) -> str:
    from pypdf import PdfReader
    r = requests.get(url, headers=UA, timeout=60)
    r.raise_for_status()
    return "\n".join((p.extract_text() or "") for p in PdfReader(io.BytesIO(r.content)).pages).strip()


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
 "step2": [{"headline": "원문 속 한 줄 사실", "thought": "이 사실에서 이어서 생각해볼 질문: '→ ...?' 형태", "why": "이 질문이 왜 나오는지, 어떤 논리로 이어지는지 풀이 2~3문장"}],
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


def render_email(d: dict, r: Report, pdf_url: str, date: dt.date) -> tuple[str, str, str]:
    """(제목, 텍스트본문, HTML본문)"""
    e = H.escape
    wd = "월화수목금토일"[date.weekday()]
    subject = f"📈 {date:%m/%d}({wd}) 모닝코멘트 — {d['one_line']}"
    cats = (("macro", "① 매크로", "금리·환율·유가·물가·연준·전쟁"),
            ("earnings", "② 실적·기업", "실적·가이던스·수주"),
            ("theme", "③ 산업·테마", "AI·반도체·전력"))

    t = [f"{date:%Y-%m-%d}({wd}) {r.title}", d["one_line"], f"원문 PDF: {pdf_url}", "", "STEP 1) 오늘 체크할 것"]
    h = [f'<div style="font-family:-apple-system,\'Malgun Gothic\',sans-serif;max-width:640px;margin:auto;color:#222;line-height:1.65">',
         f'<h2 style="margin-bottom:4px">{e(r.title)}</h2>',
         f'<div style="color:#888;font-size:13px">{date:%Y-%m-%d}({wd}) · <a href="{e(pdf_url)}">원문 PDF</a></div>',
         f'<p style="background:#f3f6ff;padding:12px 14px;border-radius:8px;font-size:16px"><b>{e(d["one_line"])}</b></p>',
         '<h3 style="border-bottom:2px solid #222;padding-bottom:4px">STEP 1) 오늘 체크할 것</h3>']
    for key, label, hint in cats:
        items = d["step1"].get(key) or []
        t += ["", f"{label} ({hint})"]
        h.append(f'<h4 style="margin:18px 0 4px">{label} <span style="font-weight:normal;color:#888;font-size:12px">{hint}</span></h4>')
        if not items:
            t.append("• 오늘은 특별한 소식 없음"); h.append('<div style="color:#aaa">오늘은 특별한 소식 없음</div>')
        for it in items:
            t += [f"• {it['fact']}", f"   → 쉽게 말하면: {it['explain']}"]
            h.append(f'<div style="margin:8px 0"><b>• {e(it["fact"])}</b><br>'
                     f'<span style="color:#444">→ 쉽게 말하면: {e(it["explain"])}</span></div>')
    t += ["", "STEP 2) 생각해볼 거리"]
    h.append('<h3 style="border-bottom:2px solid #222;padding-bottom:4px;margin-top:28px">STEP 2) 생각해볼 거리</h3>')
    for it in d["step2"]:
        t += [it["headline"], f"→ {it['thought']}", f"   왜? {it['why']}", ""]
        h.append(f'<div style="margin:12px 0"><div>{e(it["headline"])}</div>'
                 f'<div style="color:#2b5cff"><b>→ {e(it["thought"])}</b></div>'
                 f'<div style="color:#666;font-size:14px">왜? {e(it["why"])}</div></div>')
    t.append("용어 노트")
    h.append('<h3 style="border-bottom:2px solid #222;padding-bottom:4px;margin-top:28px">용어 노트</h3><ul style="padding-left:18px">')
    for g in d["glossary"]:
        t.append(f"• {g['term']}: {g['meaning']}")
        h.append(f'<li style="margin:6px 0"><b>{e(g["term"])}</b> — {e(g["meaning"])}</li>')
    h.append("</ul>")
    t += ["", f"오늘의 숙제: {d['todays_question']}"]
    h.append(f'<p style="background:#fff8e6;padding:12px 14px;border-radius:8px"><b>오늘의 숙제</b><br>{e(d["todays_question"])}</p>')
    h.append('<p style="color:#aaa;font-size:12px">AI가 만든 학습용 해설이며 투자 권유가 아닙니다. 틀릴 수 있으니 원문과 함께 보세요.</p></div>')
    return subject, "\n".join(t), "\n".join(h)


def send_mail(subject: str, text: str, html: str):
    me = os.environ["GMAIL_ADDRESS"]
    msg = MIMEMultipart("alternative")
    msg["Subject"], msg["From"], msg["To"] = subject, me, os.environ.get("MAIL_TO") or me
    msg.attach(MIMEText(text, "plain", "utf-8"))
    msg.attach(MIMEText(html, "html", "utf-8"))
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as s:
        s.login(me, os.environ["GMAIL_APP_PASSWORD"].replace(" ", ""))
        s.send_message(msg)


# ───────────────────────── main ─────────────────────────
def find_today(today: dt.date, force: bool, wait_min: int):
    """오늘자 KB 모닝코멘트를 찾는다. 아직 안 올라왔으면 wait_min 분까지 5분 간격으로 재시도."""
    deadline = time.time() + wait_min * 60
    while True:
        reports = collect_reports()
        cands = sorted((r for r in reports if is_kb_morning(r)), key=lambda r: r.date or dt.date.min, reverse=True)
        if cands and (force or cands[0].date == today):
            return cands[0], reports
        if time.time() >= deadline:
            return (cands[0] if cands else None), reports
        print(f"아직 오늘자가 없습니다. 5분 후 재시도... (수집 {len(reports)}건)")
        time.sleep(300)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true", help="오늘자가 아니어도 가장 최근 글 처리")
    ap.add_argument("--list", action="store_true", help="수집된 글 목록만 출력")
    ap.add_argument("--dry-run", action="store_true", help="메일 발송 없이 결과만 출력")
    ap.add_argument("--wait", type=int, default=0, help="오늘자가 없으면 최대 N분 기다림")
    ap.add_argument("--pdf-url", help="네이버 수집을 건너뛰고 이 PDF 로 처리(테스트용)")
    a = ap.parse_args()

    today = dt.datetime.now(KST).date()
    if a.pdf_url:
        rep, pdf_url = Report("KB 리서치 모닝코멘트(수동)", "KB증권", today, a.pdf_url, ""), a.pdf_url
    else:
        if a.list:
            reports = collect_reports()
            for r in reports:
                print(f"{r.date} | {r.broker} | {r.title} | pdf={'O' if r.pdf_url else 'X'}")
            print(f"총 {len(reports)}건, 그중 KB 모닝코멘트 {sum(map(is_kb_morning, reports))}건")
            return 0
        rep, reports = find_today(today, a.force, a.wait)
        if rep is None:
            print("KB 모닝코멘트를 찾지 못했습니다. `--list` 로 수집 결과를 확인하세요."
                  + (" (네이버 접속 자체가 안 된 듯)" if not reports else ""), file=sys.stderr)
            return 1
        if rep.date != today and not a.force:
            print(f"오늘({today}) 자료가 없습니다 (최신: {rep.date}). 휴장일이거나 미게시. 종료.")
            return 0
        pdf_url = resolve_pdf(rep)

    text = rep.content or pdf_text(pdf_url)
    if len(text) < 200:
        print("본문 텍스트가 너무 짧습니다.", file=sys.stderr)
        return 3
    d = explain(text)
    subject, body_t, body_h = render_email(d, rep, pdf_url, rep.date or today)
    if a.dry_run:
        print(subject, body_t, sep="\n\n")
        return 0
    send_mail(subject, body_t, body_h)
    print("메일 발송 완료:", subject)
    return 0


if __name__ == "__main__":
    sys.exit(main())
