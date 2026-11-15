# 모닝코멘트 자동 해설 메일 — 설정 (Secret 3개면 끝)

평일 아침 KB 리서치 모닝코멘트 → Claude 가 초보자용으로 해설 → **내 Gmail 로 발송**.

레포 → Settings → Secrets and variables → Actions → **New repository secret** 으로 아래 3개를 만드세요.

| Secret 이름 | 값 | 얻는 곳 |
|---|---|---|
| `ANTHROPIC_API_KEY` | `sk-ant-...` | console.anthropic.com → API Keys (하루 1건이라 비용 매우 적음) |
| `GMAIL_ADDRESS` | 내 Gmail 주소 | |
| `GMAIL_APP_PASSWORD` | 16자리 앱 비밀번호 | 아래 참고 |

(선택) `MAIL_TO` — 다른 주소로 받고 싶을 때. 없으면 내 Gmail 로 보냅니다.

## Gmail 앱 비밀번호 만들기 (1분)
1. https://myaccount.google.com/security 에서 **2단계 인증**이 켜져 있어야 합니다.
2. https://myaccount.google.com/apppasswords 접속 → 앱 이름 아무거나(예: `digest`) → **만들기**
3. 나오는 16자리(공백 있어도 됨)를 `GMAIL_APP_PASSWORD` 에 붙여넣기. 이 로그인용 비밀번호가 아니라 **전용 비밀번호**라 유출돼도 이것만 폐기하면 됩니다.

## 첫 실행 (네이버 구조 확인)
Actions 탭 → morning-digest → Run workflow
1. `list_only` 체크 → "KB 모닝코멘트 N건" 이 나오는지 확인
2. 정상이면 `force` 체크 후 실행 → 메일이 도착하는지 확인

이후 평일 08:40(KST)부터 자동 실행됩니다. 모닝코멘트가 올라올 때까지 최대 50분 기다렸다가 보내고, 휴장일은 조용히 종료합니다.
메일이 안 오면 Actions 실행 로그를 보내주세요.

## 문제 해결
- 수집 0건: 네이버 페이지 구조가 다른 것 → `list_only` 로그를 알려주세요.
- 메일 인증 오류: 앱 비밀번호/2단계 인증 확인. 스팸함도 확인하세요.
- "텍스트를 거의 못 뽑았습니다": 이미지형 PDF → OCR 추가 필요.
