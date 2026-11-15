# 모닝코멘트 자동 해설 — 설정 가이드

평일 아침 KB 리서치 모닝코멘트를 네이버 증권에서 가져와 → Claude가 초보자용으로 해설 →
**비공개 Google Doc 맨 위에 누적** → **카카오톡 "나에게 보내기"** 로 알림.

필요한 Secret 6개(+선택 2개)를 만들면 끝입니다.
레포 → Settings → Secrets and variables → Actions → New repository secret

## 1. Claude API 키 → `ANTHROPIC_API_KEY`
console.anthropic.com → API Keys. 하루 1건이라 비용은 매우 적습니다.

## 2. Google Doc (결과가 쌓이는 곳) → `GOOGLE_SA_JSON`, `GOOGLE_DOC_ID`
1. https://console.cloud.google.com 에서 프로젝트 생성 → "Google Docs API" 사용 설정
2. IAM 및 관리자 → 서비스 계정 만들기 → 키 탭 → 키 추가 → JSON 다운로드
   → 파일 내용 **전체**를 `GOOGLE_SA_JSON` Secret 에 붙여넣기
3. Google Docs 에서 **빈 문서**를 만들고, 공유 → 서비스 계정 이메일(`...iam.gserviceaccount.com`)을 **편집자**로 추가
   (문서는 본인만 볼 수 있는 비공개 상태 그대로 유지)
4. 문서 주소 `docs.google.com/document/d/`**`여기`**`/edit` 의 `여기` → `GOOGLE_DOC_ID`

## 3. 카카오톡 → `KAKAO_REST_API_KEY`, `KAKAO_REFRESH_TOKEN`
1. https://developers.kakao.com → 내 애플리케이션 → 앱 만들기
2. 앱 → 플랫폼 → **Web** 사이트 도메인에 `https://docs.google.com` 추가 (메시지 링크 허용용)
3. 제품 설정 → 카카오 로그인 **ON**, Redirect URI 에 `https://localhost:3000` 등록
4. 제품 설정 → 카카오 로그인 → 동의항목 → **카카오톡 메시지 전송(talk_message)** 을 선택 동의로 설정
5. 앱 키 → **REST API 키** → `KAKAO_REST_API_KEY`
   (보안 탭에서 Client Secret 을 켰다면 그 값을 `KAKAO_CLIENT_SECRET` 에 추가)
6. 브라우저에서 아래 주소를 열고(키 부분 교체) 로그인·동의 →
   주소창이 `https://localhost:3000/?code=XXXX` 로 바뀌면(페이지 오류는 정상) `XXXX` 복사
   ```
   https://kauth.kakao.com/oauth/authorize?client_id=REST_API_KEY&redirect_uri=https://localhost:3000&response_type=code&scope=talk_message
   ```
7. 터미널에서 (코드는 몇 분 안에 써야 함):
   ```
   curl -X POST https://kauth.kakao.com/oauth/token \
     -d grant_type=authorization_code -d client_id=REST_API_KEY \
     -d redirect_uri=https://localhost:3000 -d code=XXXX
   ```
   응답의 `refresh_token` → `KAKAO_REFRESH_TOKEN`

> 리프레시 토큰은 60일 유효하고, 만료 한 달 전부터 실행 때마다 새 토큰이 내려옵니다.
> 자동 저장하려면 `GH_PAT`(이 레포 Secrets 쓰기 권한이 있는 fine-grained 토큰)을 추가하세요.
> 없으면 로그에 경고가 뜨니, 그때 6~7번을 다시 하면 됩니다.

## 4. 첫 실행 (여기서 네이버 구조를 확인합니다)
Actions 탭 → morning-digest → Run workflow
1. `list_only` 체크 → 네이버에서 글이 수집되고 "KB 모닝코멘트 N건" 이 나오는지 확인
2. 정상이면 `force` 체크 후 실행 → Doc 에 해설이 쌓이고 카톡이 오는지 확인
이후엔 평일 08:50 / 09:40(KST) 에 자동 실행됩니다(이미 처리한 날은 건너뜀, 휴장일은 조용히 종료).

## 문제 해결
- 수집 0건 / "찾지 못했습니다": 네이버 페이지 구조가 달라진 것 → 로그의 `--list` 출력을 알려주세요. `LIST_URLS`/파서를 맞춥니다.
- "텍스트를 거의 못 뽑았습니다": 이미지형 PDF. OCR 단계 추가가 필요합니다.
- 카톡 401/403: 동의항목 `talk_message`, Web 도메인 등록을 확인하세요.
