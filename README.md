# Sports Matchup Analytics & Automation Pipeline (UFC)

외부 API와 내부 데이터베이스(Airtable), LLM 워크플로우(Dify)를 결합하여 스포츠 경기 데이터를 자동 수집·정규화하고, 정량/정성 하이브리드 예측 모델의 결과를 주간 단위로 자동 검증하는 파이프라인입니다.

## 1. 프로젝트 개요 및 문제 해결
* **목적:** 수작업으로 흩어진 경기 데이터를 수집·비교하던 비효율을 제거하고, 데이터 수집부터 분석 대시보드 출력 및 사후 정확도 검증까지 전 과정을 자동화
* **핵심 구조:** `외부 API / 크롤링` → `Airtable DB 적재 및 Z-Score 정규화` → `Dify(LLM) 정성 분석 결합` → `Streamlit 대시보드` → `GitHub Actions 주간 자동 검증`

## 2. 주요 기술적 구현 (Key Engineering Decisions)
* **이기종 데이터 간 개체명 매칭 (Entity Resolution):**
  * 외부 API와 내부 DB 간 선수 이름의 특수문자, 악센트, 표기 차이로 매칭이 누락되는 문제 발생
  * 유니코드 정규화(NFKD), ASCII 변환, 정규표현식 압축 및 부분 일치(Subset) 로직을 구현하고, 예외 케이스는 `manual_overrides.json`으로 수동 보정할 수 있게 설계
* **무인 자동화 및 백필(Backfill) 파이프라인:**
  * GitHub Actions(`sunday_settlement.yml`)를 활용해 매주 일요일 정해진 시간에 경기 결과를 수집하고 모델 예측 정확도를 자동 정산
  * API 장애나 누락 발생 시를 대비해 `workflow_dispatch` 기반의 기간 지정 백필(Backfill) 기능 구현
* **API 호출 최적화 및 예외 처리:**
  * Streamlit의 `@st.cache_data(ttl=3600)`를 적용해 반복적인 외부 API 및 DB 조회 비용 최소화
  * 다가오는 가장 가까운 이벤트(최초 경기 기준 18시간 이내)만 자동 필터링하며, DB 미등록 데이터 감지 시 경고 표시 및 수동 입력(Fallback) 모드 지원

## 3. Tech Stack
* **Language & UI:** Python 3.10, Streamlit
* **Database & AI:** Airtable, Dify (LLM Workflow)
* **Automation & Infra:** GitHub Actions (Cron / Workflow Dispatch), GitHub Secrets
* **External API:** The Odds API (시장 컨센서스 확률 데이터 수집용)
