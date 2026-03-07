import requests
from bs4 import BeautifulSoup
import re
import json
import math
from datetime import datetime
from pyairtable import Api
from pyairtable.formulas import match

# ================= ⚙️ 개인 설정 =================
from config import DIFY_API_KEY, AIRTABLE_TOKEN, AIRTABLE_BASE_ID
FIGHTERS_TABLE = "Fighters"
HISTORY_TABLE = "Match_History"
PREDICTIONS_TABLE = "Predictions"
# ===============================================

# 🔥 퀀트 코어 상수
HALF_LIFE_YEARS = 3.0
REACH_WEIGHT_PER_INCH = 0.008
MAX_REACH_CAP = 0.07
BASE_GRAPPLE_ADV = 0.08
MAX_GRAPPLE_CAP = 0.12

# 🏆 [V5 신규] 리그 티어 리스트
TIER_1_LEAGUES = ['bellator', 'pfl', 'strikeforce', 'wec', 'glory', 'one']
TIER_2_LEAGUES = ['ksw', 'cagewarriors', 'aca', 'rizin', 'lfa', 'invicta']

api = Api(AIRTABLE_TOKEN)
f_table = api.table(AIRTABLE_BASE_ID, FIGHTERS_TABLE)
h_table = api.table(AIRTABLE_BASE_ID, HISTORY_TABLE)
p_table = api.table(AIRTABLE_BASE_ID, PREDICTIONS_TABLE)

def get_latest_ufc_event():
    print("🌐 UFC 공식 일정 확인 중...")
    try:
        res = requests.get("http://ufcstats.com/statistics/events/completed")
        soup = BeautifulSoup(res.text, 'html.parser')
        first_event = soup.find('tr', class_='b-statistics__table-row_type_first')
        return first_event.find('a').text.strip(), first_event.find('span').text.strip()
    except Exception:
        return "Unknown Event", "Unknown Date"

def run_dify_extraction(f1, f2):
    """Dify AI: 스탠스, 스타일, 리그 정보 및 경기 정성적 시나리오 추출"""
    url = "https://api.dify.ai/v1/chat-messages"
    headers = {"Authorization": f"Bearer {DIFY_API_KEY}", "Content-Type": "application/json"}
    query = f"지식베이스에서 {f1}와(과) {f2}의 스탠스(stance), 스타일(style), pre_ufc_league 및 경기 시나리오를 추출하여 JSON 포맷으로 반환하라."
    
    data = {"inputs": {}, "query": query, "response_mode": "blocking", "user": "yoon-gi"}
    print(f"\n🧠 Dify AI 정성적 전술 추출 중... ({f1} vs {f2})")
    
    try:
        res = requests.post(url, headers=headers, json=data, timeout=60)
        res.raise_for_status()
        answer = res.json().get('answer', '')
        json_match = re.search(r'\{.*\}', answer, re.DOTALL)
        if json_match: return json.loads(json_match.group(0))
        return None
    except Exception as e:
        print(f"❌ API 통신 에러: {e}")
        return None

def calc_time_decay_weight(date_str):
    """⏳ 3년 반감기 지수 감소 방정식"""
    if not date_str: return 0.5
    try:
        match_date = datetime.strptime(date_str, '%Y-%m-%d')
        delta_years = max(0, (datetime.now() - match_date).days / 365.25)
        return math.pow(0.5, delta_years / HALF_LIFE_YEARS)
    except: return 0.5

def get_quant_data(fighter_name):
    """📊 [시즌 3 코어] 유망주(Prospect) 우회 라우팅 완벽 적용"""
    records = f_table.all(formula=match({"Name": fighter_name}))
    if not records: return None
    
    f_data = records[0]['fields']
    f_id = records[0]['id']
    
    current_ranking = f_data.get('Current_Ranking', 'Unranked')
    reach = float(f_data.get('Reach', 72.0))
    ufc_fights = int(f_data.get('UFC_Fights', 0))
    
    # 🚨 [핵심 수술] 유망주 vs 기존 선수 분기 처리
    if current_ranking == 'Prospect':
        # 1. 유망주: Match_History가 없으므로 Proxy Z-Score 사용
        tz_slpm = float(f_data.get('z_slpm', 0.0))
        tz_td = float(f_data.get('z_td_avg', 0.0))
        tz_str_def = float(f_data.get('z_str_def', 0.0))
        tz_tdd = float(f_data.get('z_tdd', 0.0))
        
        momentum = 1.0 # 갓 수집된 신인이므로 공백기 감가상각 면제
        fights = ufc_fights # 크롤링한 공식 UFC 전적 사용
    else:
        # 2. 기존 선수: 4대 핵심 True Z-Score 사용
        tz_slpm = float(f_data.get('True_z_slpm', 0.0))
        tz_td = float(f_data.get('True_z_td_avg', 0.0))
        tz_str_def = float(f_data.get('True_z_str_def', 0.0))
        tz_tdd = float(f_data.get('True_z_tdd', 0.0))
        
        matches = h_table.all(formula=f"FIND('{fighter_name}', {{Fighter}})")
        match_count = len(matches)
        fights = match_count
        
        if match_count > 0:
            valid_dates = [m['fields'].get('Date') for m in matches if m['fields'].get('Date')]
            if valid_dates:
                latest_match_date = max(valid_dates)
                momentum = calc_time_decay_weight(latest_match_date)
                
                tz_slpm *= momentum
                tz_td *= momentum
                tz_str_def *= momentum
                tz_tdd *= momentum
        else:
            tz_slpm, tz_td, tz_str_def, tz_tdd = 0.0, 0.0, 0.0, 0.0

    is_sss = fights <= 3

    return {
        "name": fighter_name.title(),
        "id": f_id,
        "tz_slpm": tz_slpm, "tz_td": tz_td,
        "tz_str_def": tz_str_def, "tz_tdd": tz_tdd,
        "reach": reach,
        "is_sss": is_sss, "fights": fights,
        "ranking": current_ranking
    }

def calculate_hybrid_model(A_quant, B_quant, A_dify, B_dify, odds_A, odds_B):
    """🔥 시즌 3 궁극 연산: True Z-Score 격차 + V5 티어별 SSS 로직"""
    
    def apply_sss_penalty(quant, dify):
        fights = quant['fights']
        
        raw_league = str(dify.get('pre_ufc_league', 'none'))
        clean_league = raw_league.lower().strip().replace(" ", "")
        
        # 1. 기본 계단식 페널티 적용 (베이지안 수축)
        if fights == 0: penalty = 0.1
        elif fights == 1: penalty = 0.4
        elif fights == 2: penalty = 0.7
        elif fights == 3: penalty = 0.9
        else: penalty = 1.0

        # 2. 💎 티어별 하이브리드 프리미엄 로직
        if fights <= 3 and clean_league != 'none':
            if any(tier in clean_league for tier in TIER_1_LEAGUES):
                penalty = min(1.0, penalty + 0.4)
                print(f"💎 [Tier 1 Premium] {quant['name']} ({raw_league.title()}) 출신! 폼 인정 (가중치: {penalty:.2f})")
            elif any(tier in clean_league for tier in TIER_2_LEAGUES):
                penalty = min(1.0, penalty + 0.2)
                print(f"🛡️ [Tier 2 Premium] {quant['name']} ({raw_league.title()}) 출신. 부분 인정 (가중치: {penalty:.2f})")
            else:
                print(f"⚠️ [Tier 3 Alert] {quant['name']} ({raw_league.title()}) 출신. 로컬 단체 거품으로 간주 (가중치: {penalty:.2f})")

        quant['tz_slpm'] *= penalty
        quant['tz_td'] *= penalty
        quant['tz_str_def'] *= penalty
        quant['tz_tdd'] *= penalty
        return quant

    # 계산 전 양 선수에게 V5 티어별 SSS 로직 적용
    A_quant = apply_sss_penalty(A_quant, A_dify)
    B_quant = apply_sss_penalty(B_quant, B_dify)

    is_A_grap = A_quant['tz_td'] > 0.5 or "Grappler" in str(A_dify.get('style', '')).title()
    is_B_grap = B_quant['tz_td'] > 0.5 or "Grappler" in str(B_dify.get('style', '')).title()
    
    if is_A_grap and is_B_grap:
        type_str, w_strike, w_grap, reach_mult = "Type A (Grap vs Grap)", 0.7, 0.3, 1.0
    elif not is_A_grap and not is_B_grap:
        type_str, w_strike, w_grap, reach_mult = "Type B (Str vs Str)", 0.9, 0.1, 1.0
    else:
        type_str, w_strike, w_grap, reach_mult = "Type C (Str vs Grap)", 0.5, 0.5, 0.5

    # 1. 🌟 기초 체력 (Volume Share)
    A_s_vol = math.exp(A_quant['tz_slpm'] - B_quant['tz_str_def'])
    A_g_vol = math.exp(A_quant['tz_td'] - B_quant['tz_tdd'])
    B_s_vol = math.exp(B_quant['tz_slpm'] - A_quant['tz_str_def'])
    B_g_vol = math.exp(B_quant['tz_td'] - A_quant['tz_tdd'])

    sum_s = A_s_vol + B_s_vol or 1
    sum_g = A_g_vol + B_g_vol or 1

    A_base = ((A_s_vol / sum_s) * w_strike) + ((A_g_vol / sum_g) * w_grap)
    B_base = ((B_s_vol / sum_s) * w_strike) + ((B_g_vol / sum_g) * w_grap)

    A_bonus, B_bonus = 0.0, 0.0

    # 2. 연속 스케일링 보너스
    reach_diff_cm = A_quant['reach'] - B_quant['reach']
    reach_diff_inch = reach_diff_cm / 2.54 
    reach_bonus = min(MAX_REACH_CAP, abs(reach_diff_inch) * REACH_WEIGHT_PER_INCH) * reach_mult
    
    if reach_diff_inch > 0: A_bonus += reach_bonus
    elif reach_diff_inch < 0: B_bonus += reach_bonus

    A_stance, B_stance = str(A_dify.get('stance', '')).lower(), str(B_dify.get('stance', '')).lower()
    stance_bonus_value = 0.025 * reach_mult 
    if A_stance == 'southpaw' and B_stance != 'southpaw': A_bonus += stance_bonus_value
    if B_stance == 'southpaw' and A_stance != 'southpaw': B_bonus += stance_bonus_value

    A_tdd_prob = 1.0 / (1.0 + math.exp(-A_quant['tz_tdd']))
    B_tdd_prob = 1.0 / (1.0 + math.exp(-B_quant['tz_tdd']))

    if "Type C" in type_str:
        if is_A_grap:
            bonus = BASE_GRAPPLE_ADV + (A_quant['tz_td'] * 0.02) - (B_tdd_prob * 0.06)
            A_bonus += max(0.00, min(MAX_GRAPPLE_CAP, bonus))
        else:
            bonus = BASE_GRAPPLE_ADV + (B_quant['tz_td'] * 0.02) - (A_tdd_prob * 0.06)
            B_bonus += max(0.00, min(MAX_GRAPPLE_CAP, bonus))

    # 3. 최종 승률 산출
    A_score = max(0.01, A_base + A_bonus)
    B_score = max(0.01, B_base + B_bonus)
    
    A_prob = A_score / (A_score + B_score)
    B_prob = B_score / (A_score + B_score)

    # 4. Kelly Criterion (자산 배분)
    A_implied, B_implied = 1/odds_A, 1/odds_B
    A_edge, B_edge = A_prob - A_implied, B_prob - B_implied
    
    target, target_prob, target_odds, target_edge = None, 0, 0, 0
    is_sss_active = A_quant['is_sss'] or B_quant['is_sss']
    
    if A_edge > 0 and A_edge > B_edge: target, target_prob, target_odds, target_edge = A_quant['name'], A_prob, odds_A, A_edge
    elif B_edge > 0 and B_edge > A_edge: target, target_prob, target_odds, target_edge = B_quant['name'], B_prob, odds_B, B_edge
    
    if target:
        if target_odds <= 1.5 and target_edge < 0.03:
            target, unit = "PASS (Trap Alert)", 0.0
        else:
            unit = ((target_odds - 1) * target_prob - (1 - target_prob)) / (target_odds - 1) / 2
            if is_sss_active: unit /= 2
            unit = max(0, round(unit, 3))
    else:
        target, unit = "PASS (No Value)", 0.0

    return {
        "A_name": A_quant['name'], "B_name": B_quant['name'], "Type": type_str,
        "A_prob": A_prob, "B_prob": B_prob, "Target": target,
        "Edge": target_edge, "Unit": unit, "SSS": is_sss_active
    }

def main():
    print("🔥 UFC HYBRID QUANT ENGINE (TRUE Z-SCORE EDITION V5) 🔥")
    event_name, event_date = get_latest_ufc_event()
    print(f"📍 대상 대회: {event_name} ({event_date})")
    
    f1 = input("\n👤 선수 A 이름 (영문): ").strip()
    o1 = float(input(f"💰 {f1} 배당률: "))
    f2 = input("👤 선수 B 이름 (영문): ").strip()
    o2 = float(input(f"💰 {f2} 배당률: "))
    
    print(f"\n⚙️ Airtable 퀀트 데이터 스캔 중...")
    A_quant = get_quant_data(f1)
    B_quant = get_quant_data(f2)
    
    if not A_quant or not B_quant:
        print("🛑 에어테이블에 선수 데이터가 없습니다. (철자 확인 요망)")
        return

    dify_json = run_dify_extraction(f1, f2)
    if not dify_json:
        print("🛑 Dify 통신 실패. 로직을 중단합니다.")
        return
        
    A_dify = dify_json.get('fighter_A', {})
    B_dify = dify_json.get('fighter_B', {})

    res = calculate_hybrid_model(A_quant, B_quant, A_dify, B_dify, o1, o2)
    
    print("\n" + "="*50)
    print(f"🥊 {res['A_name']} vs {res['B_name']}")
    print(f"📈 모델 승률: {res['A_name']} {res['A_prob']:.1%} / {res['B_name']} {res['B_prob']:.1%}")
    print(f"🎯 베팅 타겟: {res['Target']}")
    print("-" * 50)
    print(f"🎯 [Key Scenario]\n- {dify_json['analysis']['key_scenario']}")
    if res['SSS']: print("\n🚨 경고: SSS 보정 발동. 데이터 거품을 걷어내고 자산을 절반 축소했습니다.")
    print("-" * 50)
    print("💸 [Investment Strategy]")
    print(f"- 배당률: {res['Target']} ({o1 if res['Target']==res['A_name'] else o2 if res['Target']==res['B_name'] else '-'})")
    print(f"- 모델 엣지: {res['Edge']:.1%}")
    print(f"- 추천 투입 자산: {res['Unit']} Unit")
    print("-" * 50)
    print(f"⚙️ [Logic Audit] {res['Type']}")
    print("="*50)
    
    if "PASS" not in res['Target']:
        try:
            safe_event = f"{event_name} ({event_date})".replace('\n', ' ').strip()
            scenario = dify_json['analysis'].get('key_scenario', '시나리오 분석 실패')
            safe_note = f"✅ 모델 엣지: {res['Edge']:.1%}\n{scenario}"
            
            target_id = A_quant['id'] if res['Target'] == res['A_name'] else B_quant['id']
            opponent_id = B_quant['id'] if res['Target'] == res['A_name'] else A_quant['id']
            
            p_table.create({
                "Event_Name": safe_event,
                "Main_Fighter": [target_id],
                "Opponent": [opponent_id],
                "My_Win_Prob": float(round(res['A_prob'] if res['Target'] == res['A_name'] else res['B_prob'], 4)),
                "Market_Odds": float(o1 if res['Target'] == res['A_name'] else o2),
                "Bet_Amount_Unit": float(res['Unit']),
                "Analysis_Note": safe_note
            })
            print("\n✨ 에어테이블 Predictions 자동 기록 완료! (Linked Record 연동 성공)")
        except Exception as e:
            print(f"\n❌ 에어테이블 기록 실패: {e}")

if __name__ == "__main__":
    main()