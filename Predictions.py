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

# 🔥 퀀트 코어 상수 (보수적 생산 버전 + 도메인 핫픽스)
HALF_LIFE_YEARS = 2.5

REACH_WEIGHT_PER_INCH = 0.005
MAX_REACH_CAP = 0.04

BASE_GRAPPLE_ADV = 0.05
MAX_GRAPPLE_CAP = 0.08

SOUTHPAW_BONUS = 0.01
TRAP_ODDS_THRESHOLD = 1.55
TRAP_EDGE_THRESHOLD = 0.04
KELLY_DIVISOR = 4.0  # Quarter Kelly

# 🏆 리그 티어 리스트
TIER_1_LEAGUES = ['bellator', 'pfl', 'strikeforce', 'wec', 'glory', 'one']
TIER_2_LEAGUES = ['ksw', 'cagewarriors', 'aca', 'rizin', 'lfa', 'invicta']

api = Api(AIRTABLE_TOKEN)
f_table = api.table(AIRTABLE_BASE_ID, FIGHTERS_TABLE)
h_table = api.table(AIRTABLE_BASE_ID, HISTORY_TABLE)
p_table = api.table(AIRTABLE_BASE_ID, PREDICTIONS_TABLE)

_GRAPPLER_CUTOFF_CACHE = None


def safe_float(value, default=0.0):
    try:
        return float(value)
    except Exception:
        return default


def percentile(values, q):
    """q=0.9 같은 형태의 간단한 percentile"""
    vals = sorted([v for v in values if v is not None])
    if not vals:
        return 0.0
    if len(vals) == 1:
        return vals[0]

    pos = (len(vals) - 1) * q
    low = math.floor(pos)
    high = math.ceil(pos)

    if low == high:
        return vals[int(pos)]

    return vals[low] + (vals[high] - vals[low]) * (pos - low)


def get_elite_grappler_cutoffs():
    """
    로스터 전체 기준 엘리트 그래플러 컷오프 계산
    - 현재 DB 구조상 탑 컨트롤 시간은 없으므로 Sub_Avg / TD_Avg를 프록시로 사용
    """
    global _GRAPPLER_CUTOFF_CACHE
    if _GRAPPLER_CUTOFF_CACHE is not None:
        return _GRAPPLER_CUTOFF_CACHE

    try:
        records = f_table.all()
        sub_values = []
        td_values = []

        for r in records:
            fields = r.get("fields", {})
            sub_avg = safe_float(fields.get("Sub_Avg"), None)
            td_avg = safe_float(fields.get("TD_Avg"), None)

            if sub_avg is not None:
                sub_values.append(sub_avg)
            if td_avg is not None:
                td_values.append(td_avg)

        _GRAPPLER_CUTOFF_CACHE = {
            "sub_p90": percentile(sub_values, 0.90),
            "td_p85": percentile(td_values, 0.85),
            "td_p90": percentile(td_values, 0.90),
        }
        return _GRAPPLER_CUTOFF_CACHE

    except Exception:
        _GRAPPLER_CUTOFF_CACHE = {
            "sub_p90": 1.0,
            "td_p85": 2.0,
            "td_p90": 3.0,
        }
        return _GRAPPLER_CUTOFF_CACHE


def get_latest_ufc_event():
    print("🌐 UFC 공식 일정 확인 중...")
    try:
        res = requests.get("http://ufcstats.com/statistics/events/completed", timeout=30)
        soup = BeautifulSoup(res.text, 'html.parser')
        first_event = soup.find('tr', class_='b-statistics__table-row_type_first')
        return first_event.find('a').text.strip(), first_event.find('span').text.strip()
    except Exception:
        return "Unknown Event", "Unknown Date"


def is_ranked_or_champion(ranking: str) -> bool:
    if not ranking:
        return False
    r = str(ranking).lower().strip()
    return r == "champion" or r.isdigit()


def is_gatekeeper(ranking: str) -> bool:
    if not ranking:
        return False
    return "gatekeeper" in str(ranking).lower()


def is_prospect(ranking: str) -> bool:
    if not ranking:
        return False
    return str(ranking).lower().strip() == "prospect"


def is_grappling_style(style_text: str) -> bool:
    s = str(style_text).lower()
    keywords = [
        "grappler", "grappling", "wrestler", "wrestling",
        "jiu-jitsu", "jiujitsu", "bjj", "submission", "submission grappler"
    ]
    return any(k in s for k in keywords)


def detect_neutralized_grappling_scenario(scenario_text: str) -> bool:
    """
    Dify 시나리오 텍스트에서
    '상쇄형 그래플링 -> 타격전/거리전' 힌트를 감지
    """
    text = str(scenario_text).lower()

    keywords = [
        "상쇄", "타격전", "거리", "거리전", "거리 싸움", "거리싸움",
        "타격", "스탠드업", "스탠딩",
        "neutralize", "neutralized", "standup", "stand-up",
        "distance", "range", "striking", "strike", "kickboxing"
    ]

    return any(k in text for k in keywords)


def calc_time_decay_weight(date_str):
    """⏳ 2.5년 반감기 지수 감소"""
    if not date_str:
        return 0.5
    try:
        match_date = datetime.strptime(date_str, '%Y-%m-%d')
        delta_years = max(0, (datetime.now() - match_date).days / 365.25)
        return math.pow(0.5, delta_years / HALF_LIFE_YEARS)
    except Exception:
        return 0.5


def run_dify_extraction(f1, f2):
    """
    Dify AI:
    스탠스, 스타일, 리그 정보 및 경기 정성적 시나리오 추출

    ⚠️ 지식베이스 공백/정보부족 뉘앙스 감지 시
    fighter_A, fighter_B 딕셔너리에 _force_pass 플래그를 심어 반환

    ⚠️ key_scenario도 fighter_A / fighter_B 내부에 복사해서
    calculate_hybrid_model 단계에서 NLP 힌트로 재활용
    """
    url = "https://api.dify.ai/v1/chat-messages"
    headers = {
        "Authorization": f"Bearer {DIFY_API_KEY}",
        "Content-Type": "application/json"
    }
    query = (
        f"지식베이스에서 {f1}와(과) {f2}의 "
        f"스탠스(stance), 스타일(style), pre_ufc_league 및 경기 시나리오를 "
        f"JSON 포맷으로 반환하라. "
        f"단, 두 선수 모두 grappling/wrestling 계열이면 단순 그라운드 공방으로 단정하지 말고, "
        f"서로의 테이크다운이 상쇄되어 타격전/거리전으로 굳어질 가능성도 함께 반영하라."
    )

    data = {
        "inputs": {},
        "query": query,
        "response_mode": "blocking",
        "user": "yoon-gi"
    }

    print(f"\n🧠 Dify AI 정성적 전술 추출 중... ({f1} vs {f2})")

    try:
        res = requests.post(url, headers=headers, json=data, timeout=60)
        res.raise_for_status()

        answer = res.json().get("answer", "")
        answer_lower = answer.lower()

        knowledge_gap_patterns = [
            "지식베이스에 없",
            "정보가 없",
            "충분한 정보가 없",
            "데이터가 없",
            "알 수 없",
            "확인할 수 없",
            "not enough information",
            "no data",
            "insufficient information",
            "limited information",
            "knowledge base",
        ]

        direct_knowledge_gap = any(p in answer_lower for p in knowledge_gap_patterns)
        json_match = re.search(r'\{.*\}', answer, re.DOTALL)

        if not json_match and direct_knowledge_gap:
            return {
                "fighter_A": {
                    "_force_pass": True,
                    "_force_pass_reason": "Dify knowledge gap (raw text)",
                    "_key_scenario": "Dify 지식베이스 공백 감지 → 자동 베팅 보류",
                },
                "fighter_B": {
                    "_force_pass": True,
                    "_force_pass_reason": "Dify knowledge gap (raw text)",
                    "_key_scenario": "Dify 지식베이스 공백 감지 → 자동 베팅 보류",
                },
                "analysis": {
                    "key_scenario": "Dify 지식베이스 공백 감지 → 자동 베팅 보류"
                }
            }

        if not json_match:
            return None

        parsed = json.loads(json_match.group(0))

        if "fighter_A" not in parsed:
            parsed["fighter_A"] = {}
        if "fighter_B" not in parsed:
            parsed["fighter_B"] = {}
        if "analysis" not in parsed:
            parsed["analysis"] = {}

        parsed["fighter_A"].setdefault("_force_pass", False)
        parsed["fighter_A"].setdefault("_force_pass_reason", "")
        parsed["fighter_B"].setdefault("_force_pass", False)
        parsed["fighter_B"].setdefault("_force_pass_reason", "")

        scenario_value = parsed.get("analysis", {}).get("key_scenario", "")
        parsed["fighter_A"]["_key_scenario"] = scenario_value
        parsed["fighter_B"]["_key_scenario"] = scenario_value

        scenario_text = str(scenario_value).lower()

        weak_a = not any(parsed["fighter_A"].get(k) for k in ["stance", "style", "pre_ufc_league"])
        weak_b = not any(parsed["fighter_B"].get(k) for k in ["stance", "style", "pre_ufc_league"])

        vague_patterns = [
            "정보 부족",
            "충분한 정보",
            "알 수 없다",
            "확인할 수 없다",
            "limited information",
            "insufficient information",
            "not enough information",
            "no data",
        ]

        structural_knowledge_gap = (weak_a or weak_b) and any(v in scenario_text for v in vague_patterns)

        if direct_knowledge_gap or structural_knowledge_gap:
            parsed["fighter_A"]["_force_pass"] = True
            parsed["fighter_A"]["_force_pass_reason"] = "Dify knowledge gap"
            parsed["fighter_B"]["_force_pass"] = True
            parsed["fighter_B"]["_force_pass_reason"] = "Dify knowledge gap"

            if not parsed["analysis"].get("key_scenario"):
                parsed["analysis"]["key_scenario"] = "Dify 지식베이스 공백 감지 → 자동 베팅 보류"

            parsed["fighter_A"]["_key_scenario"] = parsed["analysis"]["key_scenario"]
            parsed["fighter_B"]["_key_scenario"] = parsed["analysis"]["key_scenario"]

        return parsed

    except Exception as e:
        print(f"❌ API 통신 에러: {e}")
        return None


def get_quant_data(fighter_name):
    """📊 퀀트 데이터 로드 + 메타정보 포함"""
    records = f_table.all(formula=match({"Name": fighter_name}))
    if not records:
        return None

    f_data = records[0]["fields"]
    f_id = records[0]["id"]

    current_ranking = f_data.get("Current_Ranking", "Unranked")
    reach = float(f_data.get("Reach", 72.0))
    ufc_fights = int(f_data.get("UFC_Fights", 0))

    raw_td_avg = safe_float(f_data.get("TD_Avg"), 0.0)
    raw_sub_avg = safe_float(f_data.get("Sub_Avg"), 0.0)

    if current_ranking == "Prospect":
        tz_slpm = float(f_data.get("z_slpm", 0.0))
        tz_td = float(f_data.get("z_td_avg", 0.0))
        tz_str_def = float(f_data.get("z_str_def", 0.0))
        tz_tdd = float(f_data.get("z_tdd", 0.0))

        fights = ufc_fights

    else:
        tz_slpm = float(f_data.get("True_z_slpm", 0.0))
        tz_td = float(f_data.get("True_z_td_avg", 0.0))
        tz_str_def = float(f_data.get("True_z_str_def", 0.0))
        tz_tdd = float(f_data.get("True_z_tdd", 0.0))

        matches = h_table.all(formula=f"FIND('{fighter_name}', {{Fighter}})")
        match_count = len(matches)
        fights = match_count

        if match_count > 0:
            valid_dates = [m["fields"].get("Date") for m in matches if m["fields"].get("Date")]
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
        "tz_slpm": tz_slpm,
        "tz_td": tz_td,
        "tz_str_def": tz_str_def,
        "tz_tdd": tz_tdd,
        "reach": reach,
        "is_sss": is_sss,
        "fights": fights,
        "ranking": current_ranking,
        "is_prospect": is_prospect(current_ranking),
        "is_gatekeeper": is_gatekeeper(current_ranking),
        "is_ranked": is_ranked_or_champion(current_ranking),
        "raw_td_avg": raw_td_avg,
        "raw_sub_avg": raw_sub_avg,
    }


def apply_sss_penalty(quant, dify):
    """
    소표본(SSS) 신뢰도 패널티
    - 리그 프리미엄은 주되, 면죄부는 주지 않음
    """
    fights = quant["fights"]

    raw_league = str(dify.get("pre_ufc_league", "none"))
    clean_league = raw_league.lower().strip().replace(" ", "")

    if fights == 0:
        penalty = 0.15
    elif fights == 1:
        penalty = 0.35
    elif fights == 2:
        penalty = 0.55
    elif fights == 3:
        penalty = 0.75
    else:
        penalty = 1.0

    if fights <= 3 and clean_league != "none":
        if any(tier in clean_league for tier in TIER_1_LEAGUES):
            penalty += 0.15
            print(f"💎 [Tier 1 Premium] {quant['name']} ({raw_league.title()}) → 보정치 +0.15")
        elif any(tier in clean_league for tier in TIER_2_LEAGUES):
            penalty += 0.08
            print(f"🛡️ [Tier 2 Premium] {quant['name']} ({raw_league.title()}) → 보정치 +0.08")
        else:
            print(f"⚠️ [Tier 3 Alert] {quant['name']} ({raw_league.title()}) → 로컬 단체로 간주")

    if fights <= 3:
        penalty = min(penalty, 0.85)

    quant["tz_slpm"] *= penalty
    quant["tz_td"] *= penalty
    quant["tz_str_def"] *= penalty
    quant["tz_tdd"] *= penalty

    return quant


def apply_unit_cap(unit, A_quant, B_quant):
    """
    추천 Unit 하드캡
    - 불확실한 경기에서 돈이 과도하게 나가지 않도록 막는다
    """
    cap = None
    min_fights = min(A_quant["fights"], B_quant["fights"])

    if min_fights <= 1:
        cap = 0.02
    elif min_fights <= 3:
        cap = 0.04

    has_prospect = A_quant["is_prospect"] or B_quant["is_prospect"]
    has_veteran = (
        A_quant["is_ranked"]
        or B_quant["is_ranked"]
        or A_quant["is_gatekeeper"]
        or B_quant["is_gatekeeper"]
    )

    if has_prospect and has_veteran:
        if cap is None:
            cap = 0.03
        else:
            cap = min(cap, 0.03)

    return min(unit, cap) if cap is not None else unit


def is_elite_grappler(quant, dify):
    """
    엘리트 그래플러 판정
    - 현재 DB에는 탑 컨트롤 시간이 없어서 Sub_Avg + TD_Avg를 프록시로 사용
    """
    cutoffs = get_elite_grappler_cutoffs()
    style_flag = is_grappling_style(dify.get("style", ""))

    sub_flag = quant["raw_sub_avg"] >= cutoffs["sub_p90"] if cutoffs["sub_p90"] > 0 else False
    td_flag = (
        quant["raw_td_avg"] >= cutoffs["td_p90"]
        or (quant["raw_td_avg"] >= cutoffs["td_p85"] and quant["tz_td"] >= 0.75)
    )

    return style_flag and (sub_flag or td_flag)


def get_grappling_suppression_discount(quant):
    """
    엘리트 그래플러가 상대 타격 볼륨을 죽이는 할인율
    - 30~50% 범위
    """
    cutoffs = get_elite_grappler_cutoffs()

    if quant["raw_td_avg"] >= cutoffs["td_p90"]:
        return 0.50
    elif quant["raw_td_avg"] >= cutoffs["td_p85"]:
        return 0.40
    else:
        return 0.30


def calculate_hybrid_model(A_quant, B_quant, A_dify, B_dify, odds_A, odds_B):
    """
    하이브리드 모델
    - 소표본 패널티 적용
    - 지식 공백 시 Force Pass
    - 그래플러 vs 그래플러 상쇄형 타격전 반영 (NLP + 정량 하이브리드)
    - 엘리트 그래플러 상대로는 TDD 신뢰도와 SLpM을 할인
    - Unit은 보수적으로 캡
    """

    # 0. Dify 지식 공백 강제 PASS
    if A_dify.get("_force_pass") or B_dify.get("_force_pass"):
        return {
            "A_name": A_quant["name"],
            "B_name": B_quant["name"],
            "Type": "Force Pass (Dify Knowledge Gap)",
            "A_prob": 0.50,
            "B_prob": 0.50,
            "Target": "PASS (Dify Knowledge Gap)",
            "Edge": 0.0,
            "Unit": 0.0,
            "SSS": A_quant["is_sss"] or B_quant["is_sss"],
        }

    # 1. SSS 패널티 적용
    A_quant = apply_sss_penalty(A_quant, A_dify)
    B_quant = apply_sss_penalty(B_quant, B_dify)

    A_style_grap = is_grappling_style(A_dify.get("style", ""))
    B_style_grap = is_grappling_style(B_dify.get("style", ""))

    is_A_grap = A_quant["tz_td"] > 0.5 or A_style_grap
    is_B_grap = B_quant["tz_td"] > 0.5 or B_style_grap

    # 2. 엘리트 그래플러 패널티 적용
    A_is_elite_grappler = is_elite_grappler(A_quant, A_dify)
    B_is_elite_grappler = is_elite_grappler(B_quant, B_dify)

    if A_is_elite_grappler:
        B_quant["tz_tdd"] *= 0.70
        print(f"🧨 [Elite Grappler Penalty] {A_quant['name']}이(가) 엘리트 그래플러로 판정되어 {B_quant['name']}의 TDD 신뢰도 30% 삭감")

        if not is_B_grap:
            discount = get_grappling_suppression_discount(A_quant)
            B_quant["tz_slpm"] *= (1 - discount)
            print(f"🔒 [Grappling Suppression] {A_quant['name']} 영향으로 {B_quant['name']}의 SLpM {int(discount * 100)}% 할인")

    if B_is_elite_grappler:
        A_quant["tz_tdd"] *= 0.70
        print(f"🧨 [Elite Grappler Penalty] {B_quant['name']}이(가) 엘리트 그래플러로 판정되어 {A_quant['name']}의 TDD 신뢰도 30% 삭감")

        if not is_A_grap:
            discount = get_grappling_suppression_discount(B_quant)
            A_quant["tz_slpm"] *= (1 - discount)
            print(f"🔒 [Grappling Suppression] {B_quant['name']} 영향으로 {A_quant['name']}의 SLpM {int(discount * 100)}% 할인")

    # 3. Grap vs Grap 상쇄형 타격전 NLP + 정량 하이브리드 분기
    scenario_hint_text = f"{A_dify.get('_key_scenario', '')} {B_dify.get('_key_scenario', '')}"
    nlp_neutralization_hint = detect_neutralized_grappling_scenario(scenario_hint_text)

    quantitative_neutralization = (
        (A_quant["tz_tdd"] > 0 and B_quant["tz_tdd"] > 0)
        or (A_quant["tz_td"] > 0.75 and B_quant["tz_td"] > 0.75)
    )

    mutual_takedown_neutralization = (
        is_A_grap
        and is_B_grap
        and (quantitative_neutralization or nlp_neutralization_hint)
    )

    if mutual_takedown_neutralization:
        type_str = "Type A2 (Neutralized Grap vs Grap)"
        raw_w_strike = 0.7 * 1.5
        raw_w_grap = 0.3
        total = raw_w_strike + raw_w_grap
        w_strike = raw_w_strike / total
        w_grap = raw_w_grap / total
        reach_mult = 1.0

        if nlp_neutralization_hint:
            print("🧠 [NLP Override] Dify 시나리오에서 상쇄형 그래플링/타격전 힌트 감지 → Type A2 강제 분기")

    elif is_A_grap and is_B_grap:
        type_str, w_strike, w_grap, reach_mult = "Type A (Grap vs Grap)", 0.7, 0.3, 1.0

    elif not is_A_grap and not is_B_grap:
        type_str, w_strike, w_grap, reach_mult = "Type B (Str vs Str)", 0.9, 0.1, 1.0

    else:
        type_str, w_strike, w_grap, reach_mult = "Type C (Str vs Grap)", 0.5, 0.5, 0.5

    # 4. 기초 체력 (Volume Share)
    A_s_vol = math.exp(A_quant["tz_slpm"] - B_quant["tz_str_def"])
    A_g_vol = math.exp(A_quant["tz_td"] - B_quant["tz_tdd"])
    B_s_vol = math.exp(B_quant["tz_slpm"] - A_quant["tz_str_def"])
    B_g_vol = math.exp(B_quant["tz_td"] - A_quant["tz_tdd"])

    sum_s = A_s_vol + B_s_vol or 1
    sum_g = A_g_vol + B_g_vol or 1

    A_base = ((A_s_vol / sum_s) * w_strike) + ((A_g_vol / sum_g) * w_grap)
    B_base = ((B_s_vol / sum_s) * w_strike) + ((B_g_vol / sum_g) * w_grap)

    A_bonus, B_bonus = 0.0, 0.0

    # 5. 리치 보너스
    reach_diff_cm = A_quant["reach"] - B_quant["reach"]
    reach_diff_inch = reach_diff_cm / 2.54
    reach_bonus = min(MAX_REACH_CAP, abs(reach_diff_inch) * REACH_WEIGHT_PER_INCH) * reach_mult

    if reach_diff_inch > 0:
        A_bonus += reach_bonus
    elif reach_diff_inch < 0:
        B_bonus += reach_bonus

    # 6. 사우스포 보너스
    A_stance = str(A_dify.get("stance", "")).lower()
    B_stance = str(B_dify.get("stance", "")).lower()

    if A_stance == "southpaw" and B_stance != "southpaw":
        A_bonus += SOUTHPAW_BONUS * reach_mult
    if B_stance == "southpaw" and A_stance != "southpaw":
        B_bonus += SOUTHPAW_BONUS * reach_mult

    # 7. 그래플링 상성 보너스
    A_tdd_prob = 1.0 / (1.0 + math.exp(-A_quant["tz_tdd"]))
    B_tdd_prob = 1.0 / (1.0 + math.exp(-B_quant["tz_tdd"]))

    if "Type C" in type_str:
        if is_A_grap:
            bonus = BASE_GRAPPLE_ADV + (A_quant["tz_td"] * 0.015) - (B_tdd_prob * 0.04)
            A_bonus += max(0.00, min(MAX_GRAPPLE_CAP, bonus))
        else:
            bonus = BASE_GRAPPLE_ADV + (B_quant["tz_td"] * 0.015) - (A_tdd_prob * 0.04)
            B_bonus += max(0.00, min(MAX_GRAPPLE_CAP, bonus))

    # 8. 최종 승률 산출
    A_score = max(0.01, A_base + A_bonus)
    B_score = max(0.01, B_base + B_bonus)

    A_prob = A_score / (A_score + B_score)
    B_prob = B_score / (A_score + B_score)

    # 9. Kelly Criterion (보수적 버전)
    A_implied = 1 / odds_A
    B_implied = 1 / odds_B
    A_edge = A_prob - A_implied
    B_edge = B_prob - B_implied

    target, target_prob, target_odds, target_edge = None, 0, 0, 0
    is_sss_active = A_quant["is_sss"] or B_quant["is_sss"]

    if A_edge > 0 and A_edge > B_edge:
        target, target_prob, target_odds, target_edge = A_quant["name"], A_prob, odds_A, A_edge
    elif B_edge > 0 and B_edge > A_edge:
        target, target_prob, target_odds, target_edge = B_quant["name"], B_prob, odds_B, B_edge

    if target:
        if target_odds <= TRAP_ODDS_THRESHOLD and target_edge < TRAP_EDGE_THRESHOLD:
            target, unit = "PASS (Trap Alert)", 0.0

        else:
            raw_kelly = ((target_odds - 1) * target_prob - (1 - target_prob)) / (target_odds - 1)
            unit = raw_kelly / KELLY_DIVISOR
            unit = max(0.0, unit)

            unit = apply_unit_cap(unit, A_quant, B_quant)
            unit = round(unit, 3)

            if unit <= 0:
                target, unit = "PASS (Unit Too Small)", 0.0
    else:
        target, unit = "PASS (No Value)", 0.0

    return {
        "A_name": A_quant["name"],
        "B_name": B_quant["name"],
        "Type": type_str,
        "A_prob": A_prob,
        "B_prob": B_prob,
        "Target": target,
        "Edge": target_edge,
        "Unit": unit,
        "SSS": is_sss_active,
    }


def main():
    print("🔥 UFC HY কতটা QUANT ENGINE (SAFE PRODUCTION VERSION V1.2) 🔥")
    event_name, event_date = get_latest_ufc_event()
    print(f"📍 대상 대회: {event_name} ({event_date})")

    f1 = input("\n👤 선수 A 이름 (영문): ").strip()
    o1 = float(input(f"💰 {f1} 배당률: "))
    f2 = input("👤 선수 B 이름 (영문): ").strip()
    o2 = float(input(f"💰 {f2} 배당률: "))

    print("\n⚙️ Airtable 퀀트 데이터 스캔 중...")
    A_quant = get_quant_data(f1)
    B_quant = get_quant_data(f2)

    if not A_quant or not B_quant:
        print("🛑 에어테이블에 선수 데이터가 없습니다. (철자 확인 요망)")
        return

    dify_json = run_dify_extraction(f1, f2)
    if not dify_json:
        print("🛑 Dify 통신 실패 또는 JSON 파싱 실패. 로직을 중단합니다.")
        return

    A_dify = dify_json.get("fighter_A", {})
    B_dify = dify_json.get("fighter_B", {})

    res = calculate_hybrid_model(A_quant, B_quant, A_dify, B_dify, o1, o2)

    print("\n" + "=" * 50)
    print(f"🥊 {res['A_name']} vs {res['B_name']}")
    print(f"📈 모델 승률: {res['A_name']} {res['A_prob']:.1%} / {res['B_name']} {res['B_prob']:.1%}")
    print(f"🎯 베팅 타겟: {res['Target']}")
    print("-" * 50)
    print(f"🎯 [Key Scenario]\n- {dify_json.get('analysis', {}).get('key_scenario', '시나리오 없음')}")

    if res["SSS"]:
        print("\n🚨 경고: SSS 보정 발동. 소표본 선수 포함 경기이므로 보수적으로 계산했습니다.")

    print("-" * 50)
    print("💸 [Investment Strategy]")
    print(f"- 모델 엣지: {res['Edge']:.1%}")
    print(f"- 추천 투입 자산: {res['Unit']} Unit")
    print("-" * 50)
    print(f"⚙️ [Logic Audit] {res['Type']}")
    print("=" * 50)

    if "PASS" not in res["Target"]:
        try:
            safe_event = f"{event_name} ({event_date})".replace("\n", " ").strip()
            scenario = dify_json.get("analysis", {}).get("key_scenario", "시나리오 분석 실패")
            safe_note = f"✅ 모델 엣지: {res['Edge']:.1%}\n{scenario}"

            target_id = A_quant["id"] if res["Target"] == res["A_name"] else B_quant["id"]
            opponent_id = B_quant["id"] if res["Target"] == res["A_name"] else A_quant["id"]

            p_table.create(
                {
                    "Event_Name": safe_event,
                    "Main_Fighter": [target_id],
                    "Opponent": [opponent_id],
                    "My_Win_Prob": float(round(res["A_prob"] if res["Target"] == res["A_name"] else res["B_prob"], 4)),
                    "Market_Odds": float(o1 if res["Target"] == res["A_name"] else o2),
                    "Bet_Amount_Unit": float(res["Unit"]),
                    "Analysis_Note": safe_note,
                }
            )
            print("\n✨ 에어테이블 Predictions 자동 기록 완료! (Linked Record 연동 성공)")

        except Exception as e:
            print(f"\n❌ 에어테이블 기록 실패: {e}")


if __name__ == "__main__":
    main()