import streamlit as st
import Predictions as pred
import requests
import unicodedata
import re
from datetime import datetime, timedelta, timezone

# ==========================================
# 🔑 여기에 본인 The Odds API 키를 넣어!
# ==========================================
API_KEY = "2fea149fa0cdc00efe539cabc8dc34e5"

# --- 🛠️ 이름 정제 및 매칭 코어 로직 ---
def get_unified_name(name):
    """표기 오류 및 동명이인 방어를 위한 유니파이드 압축"""
    if not name: return ""
    n = name.lower()
    n = unicodedata.normalize('NFKD', n).encode('ASCII', 'ignore').decode('utf-8')
    n = re.sub(r'[^a-z0-9]', '', n)
    return n

@st.cache_data(ttl=3600)
def get_airtable_fighters_map():
    """에어테이블 명단을 {압축이름: {'name': 진짜이름, 'ranking': 계급}} 형태로 캐싱"""
    try:
        records = pred.f_table.all()
        f_map = {}
        for r in records:
            orig_name = r['fields'].get('Name')
            ranking = r['fields'].get('Current_Ranking', 'Unranked')
            if orig_name:
                f_map[get_unified_name(orig_name)] = {"name": orig_name, "ranking": ranking}
        return f_map
    except Exception as e:
        return {}

def find_matching_fighter(api_name, db_map):
    """API 이름과 DB 이름을 비교해서 일치하는 선수의 딕셔너리 정보 반환"""
    api_uni = get_unified_name(api_name)
    
    # 1. 100% 일치
    if api_uni in db_map: return db_map[api_uni]
    
    # 2. 포함(Subset) 매칭
    for db_uni, data in db_map.items():
        if len(db_uni) >= 5 and len(api_uni) >= 5:
            if db_uni in api_uni or api_uni in db_uni:
                return data
    return None

def get_kor_category(ranking_str):
    """에어테이블 랭킹 영문을 직관적인 한글 카테고리로 변환"""
    if not ranking_str: return "언랭크"
    r = str(ranking_str).lower()
    
    if r == "champion" or r.isdigit(): return "랭커"
    if "gatekeeper" in r: return "수문장"
    if "prospect" in r: return "유망주"
    return "언랭크"

# --- 🛡️ API 무인 정찰기 & 자동 필터링 (가시성 극대화) ---
@st.cache_data(ttl=3600) 
def get_upcoming_matchups():
    url = f"https://api.the-odds-api.com/v4/sports/mma_mixed_martial_arts/odds/"
    params = {"apiKey": API_KEY, "regions": "us", "markets": "h2h", "oddsFormat": "decimal"}
    
    try:
        res = requests.get(url, params=params)
        if res.status_code != 200: return {"❌ API 통신 에러": {"fighter_a": "", "fighter_b": "", "odds_a": 1.5, "odds_b": 2.5}}
        
        data = res.json()
        matchups = {}
        db_map = get_airtable_fighters_map()
        
        matchups["✍️ 직접 선수 이름과 배당률 텍스트로 치기 (수동 모드)"] = {
            "fighter_a": "", "fighter_b": "", "odds_a": 1.50, "odds_b": 2.50
        }
        
        now = datetime.now(timezone.utc)
        
        for event in data:
            # 8일 이내 경기만 필터링
            commence_time_str = event.get('commence_time', '')
            if commence_time_str:
                try:
                    match_time = datetime.fromisoformat(commence_time_str.replace('Z', '+00:00'))
                    if match_time > now + timedelta(days=8): continue
                except Exception: pass
            
            f_a_api = event.get('home_team', '')
            f_b_api = event.get('away_team', '')
            
            f_a_data = find_matching_fighter(f_a_api, db_map)
            f_b_data = find_matching_fighter(f_b_api, db_map)
            
            final_a = f_a_data["name"] if f_a_data else f_a_api
            final_b = f_b_data["name"] if f_b_data else f_b_api
            
            # 🎯 계급 정보 가시성 확보 로직 추가
            if f_a_data and f_b_data:
                cat_a = get_kor_category(f_a_data["ranking"])
                cat_b = get_kor_category(f_b_data["ranking"])
                title = f"🌟 [{cat_a} vs {cat_b}] {final_a} vs {final_b}"
                
            elif f_a_data: # B가 DB에 없음
                cat_a = get_kor_category(f_a_data["ranking"])
                title = f"⚠️ [데이터 부족] [{cat_a}] {final_a} vs 🚨{final_b}(DB없음)"
                
            elif f_b_data: # A가 DB에 없음
                cat_b = get_kor_category(f_b_data["ranking"])
                title = f"⚠️ [데이터 부족] 🚨{final_a}(DB없음) vs [{cat_b}] {final_b}"
                
            else: 
                title = f"👻 [스킵/신인전] {final_a} vs {final_b}"
            
            odds_a, odds_b = 1.85, 1.85 
            if event.get('bookmakers'):
                markets = event['bookmakers'][0].get('markets', [])
                if markets:
                    for out in markets[0].get('outcomes', []):
                        if out['name'] == f_a_api: odds_a = out['price']
                        elif out['name'] == f_b_api: odds_b = out['price']
            
            matchups[title] = {
                "fighter_a": final_a, "fighter_b": final_b, "odds_a": float(odds_a), "odds_b": float(odds_b)
            }
            
        # 정렬 로직 (이모지 기반)
        sorted_matchups = {k: v for k, v in sorted(matchups.items(), key=lambda item: (
            0 if "수동 모드" in item[0] else 1 if "🌟" in item[0] else 2 if "⚠️" in item[0] else 3
        ))}
            
        return sorted_matchups
    except Exception as e:
        return {"❌ API 파싱 실패": {"fighter_a": "", "fighter_b": "", "odds_a": 1.5, "odds_b": 2.5}}

# --- 🖥️ UI 화면 구성 ---
st.set_page_config(page_title="UFC Quant Fund", page_icon="📈", layout="centered")

st.title("🔥 UFC Hybrid Quant Engine V5.4")
st.markdown("### 🤖 실시간 배당률 & 타겟 스마트 필터링 대시보드")
st.divider()

matchups_dict = get_upcoming_matchups()
selected_match = st.selectbox("🎯 이번 주 타겟 매치업 선택", list(matchups_dict.keys()))
default_data = matchups_dict[selected_match]

col1, col2 = st.columns(2)
with col1:
    fighter_a = st.text_input("👤 선수 A (Top Dog)", value=default_data["fighter_a"])
    odds_a = st.number_input("💰 선수 A 배당률", min_value=1.01, value=default_data["odds_a"], step=0.01, format="%.2f")

with col2:
    fighter_b = st.text_input("👤 선수 B (Underdog)", value=default_data["fighter_b"])
    odds_b = st.number_input("💰 선수 B 배당률", min_value=1.01, value=default_data["odds_b"], step=0.01, format="%.2f")

st.caption("💡 무인 정찰기가 이번 주 경기만 싹 긁어왔어. 🚨(DB없음) 표시가 뜬 선수는 크롤러로 먼저 수집해야 엔진이 돌아가!")
st.divider()

# --- 🚀 백그라운드 연산 엔진 ---
if st.button("🚀 퀀트 엔진 가동 (분석 시작)", use_container_width=True):
    if fighter_a and fighter_b:
        with st.spinner('⚙️ 에어테이블 스캔 및 Dify AI 전술 추출 중... (약 10~20초 소요)'):
            A_quant = pred.get_quant_data(fighter_a.strip())
            B_quant = pred.get_quant_data(fighter_b.strip())

            if not A_quant or not B_quant:
                st.error("🛑 에어테이블에 선수 데이터가 없어. 대시보드에서 🚨(DB없음)이 뜬 선수를 먼저 크롤링해 줘.")
            else:
                dify_json = pred.run_dify_extraction(fighter_a.strip(), fighter_b.strip())
                if not dify_json: st.error("🛑 Dify 통신 실패.")
                else:
                    res = pred.calculate_hybrid_model(A_quant, B_quant, dify_json.get('fighter_A', {}), dify_json.get('fighter_B', {}), odds_a, odds_b)

                    st.success(f"✅ 분석 완료! 최종 타겟: **{res['Target']}**")
                    st.markdown("### 📊 모델 승률 및 엣지")
                    c1, c2, c3 = st.columns(3)
                    c1.metric(label=f"{res['A_name']} 승률", value=f"{res['A_prob']:.1%}")
                    c2.metric(label=f"{res['B_name']} 승률", value=f"{res['B_prob']:.1%}")
                    c3.metric(label="🌟 모델 엣지", value=f"{res['Edge']:.1%}")

                    st.markdown("### 💸 투자 전략")
                    st.info(f"**추천 투입 자산:** {res['Unit']} Unit")
                    if res['SSS']: st.warning("🚨 경고: SSS 보정 발동. 데이터 거품을 걷어내고 자산을 절반 축소했어.")
                    st.markdown("### 🧠 경기 시나리오")
                    st.write(dify_json['analysis']['key_scenario'])
                    st.caption(f"⚙️ Logic Audit: {res['Type']}")
    else:
        st.error("🚨 두 선수의 이름을 모두 입력해야 해.")