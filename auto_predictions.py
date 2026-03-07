import requests
import unicodedata
import re
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

from pyairtable.formulas import match

import Predictions as pred
from config import ODDS_API_KEY

# ==========================================
# ⚙️ 자동 배치 실행용 설정
# ==========================================
SPORT = "mma_mixed_martial_arts"
LOOKAHEAD_DAYS = 8  # 이번 주 경기만 분석


def get_unified_name(name: str) -> str:
    """표기 오류 방어용: 이름을 영문/숫자만 남긴 압축 키로 변환"""
    if not name:
        return ""
    n = name.lower()
    n = unicodedata.normalize('NFKD', n).encode('ASCII', 'ignore').decode('utf-8')
    n = re.sub(r'[^a-z0-9]', '', n)
    return n


def get_airtable_fighters_map() -> Dict[str, Dict[str, str]]:
    """Airtable Fighters를 {압축이름: {name, ranking, id}} 형태로 캐싱"""
    records = pred.f_table.all()
    f_map = {}
    for r in records:
        fields = r.get("fields", {})
        orig_name = fields.get("Name")
        ranking = fields.get("Current_Ranking", "Unranked")
        if orig_name:
            f_map[get_unified_name(orig_name)] = {
                "name": orig_name,
                "ranking": ranking,
                "id": r["id"],
            }
    return f_map


def find_matching_fighter(api_name: str, db_map: Dict[str, Dict[str, str]]) -> Optional[Dict[str, str]]:
    """API 이름과 DB 이름을 비교해서 가장 가까운 선수를 반환"""
    api_uni = get_unified_name(api_name)

    # 1. 100% 일치
    if api_uni in db_map:
        return db_map[api_uni]

    # 2. 포함(Subset) 매칭
    for db_uni, data in db_map.items():
        if len(db_uni) >= 5 and len(api_uni) >= 5:
            if db_uni in api_uni or api_uni in db_uni:
                return data
    return None


def get_upcoming_matchups() -> List[Dict]:
    """The Odds API에서 이번 주 UFC/MMA 매치업을 가져오고 DB 선수명으로 정규화"""
    url = f"https://api.the-odds-api.com/v4/sports/{SPORT}/odds/"
    params = {
        "apiKey": ODDS_API_KEY,
        "regions": "us",
        "markets": "h2h",
        "oddsFormat": "decimal",
    }

    print("🌐 이번 주 배당률 매치업 불러오는 중...")
    res = requests.get(url, params=params, timeout=30)
    res.raise_for_status()
    data = res.json()

    db_map = get_airtable_fighters_map()
    now = datetime.now(timezone.utc)
    matchups = []

    for event in data:
        commence_time_str = event.get("commence_time", "")
        if commence_time_str:
            try:
                match_time = datetime.fromisoformat(commence_time_str.replace("Z", "+00:00"))
                if match_time > now + timedelta(days=LOOKAHEAD_DAYS):
                    continue
            except Exception:
                match_time = None
        else:
            match_time = None

        f_a_api = event.get("home_team", "")
        f_b_api = event.get("away_team", "")
        if not f_a_api or not f_b_api:
            continue

        f_a_data = find_matching_fighter(f_a_api, db_map)
        f_b_data = find_matching_fighter(f_b_api, db_map)

        # DB에 둘 다 없는 경기면 자동 예측 품질이 너무 떨어지니 스킵
        if not f_a_data or not f_b_data:
            print(f"⚠️ 스킵: DB 매칭 불완전 -> {f_a_api} vs {f_b_api}")
            continue

        odds_a, odds_b = None, None
        if event.get("bookmakers"):
            markets = event["bookmakers"][0].get("markets", [])
            if markets:
                for out in markets[0].get("outcomes", []):
                    if out["name"] == f_a_api:
                        odds_a = out["price"]
                    elif out["name"] == f_b_api:
                        odds_b = out["price"]

        if odds_a is None or odds_b is None:
            print(f"⚠️ 스킵: 배당률 없음 -> {f_a_api} vs {f_b_api}")
            continue

        event_label_time = commence_time_str.replace("T", " ").replace("Z", " UTC") if commence_time_str else "시간미정"
        event_name = f"AUTO | {f_a_data['name']} vs {f_b_data['name']} | {event_label_time}"

        matchups.append(
            {
                "event_name": event_name,
                "fighter_a": f_a_data["name"],
                "fighter_b": f_b_data["name"],
                "fighter_a_id": f_a_data["id"],
                "fighter_b_id": f_b_data["id"],
                "odds_a": float(odds_a),
                "odds_b": float(odds_b),
            }
        )

    print(f"✅ 자동 분석 대상 경기 {len(matchups)}개 확보 완료")
    return matchups


def prediction_exists(event_name: str, target_id: str, opponent_id: str) -> bool:
    """같은 이벤트/같은 페어가 이미 Predictions에 있으면 중복 저장 방지"""
    try:
        records = pred.p_table.all(formula=match({"Event_Name": event_name}))
        for r in records:
            fields = r.get("fields", {})
            main_ids = fields.get("Main_Fighter", [])
            opp_ids = fields.get("Opponent", [])
            if main_ids and opp_ids and main_ids[0] == target_id and opp_ids[0] == opponent_id:
                return True
    except Exception as e:
        print(f"⚠️ 중복 체크 중 에러(일단 계속 진행): {e}")
    return False


def save_prediction(event_name: str, res: Dict, dify_json: Dict, A_quant: Dict, B_quant: Dict, odds_a: float, odds_b: float) -> bool:
    """수동 Predictions.py와 동일한 필드 구조로 Airtable Predictions 저장"""
    if "PASS" in res["Target"]:
        print(f"⏭️ PASS 경기 스킵: {A_quant['name']} vs {B_quant['name']} -> {res['Target']}")
        return False

    try:
        scenario = dify_json.get("analysis", {}).get("key_scenario", "시나리오 분석 실패")
        safe_note = f"✅ 모델 엣지: {res['Edge']:.1%}\n{scenario}"

        if res["Target"] == res["A_name"]:
            target_id = A_quant["id"]
            opponent_id = B_quant["id"]
            target_prob = res["A_prob"]
            target_odds = odds_a
        else:
            target_id = B_quant["id"]
            opponent_id = A_quant["id"]
            target_prob = res["B_prob"]
            target_odds = odds_b

        if prediction_exists(event_name, target_id, opponent_id):
            print(f"⚠️ 이미 저장된 예측이라 스킵: {event_name} / {res['Target']}")
            return False

        pred.p_table.create(
            {
                "Event_Name": event_name,
                "Main_Fighter": [target_id],
                "Opponent": [opponent_id],
                "My_Win_Prob": float(round(target_prob, 4)),
                "Market_Odds": float(target_odds),
                "Bet_Amount_Unit": float(res["Unit"]),
                "Analysis_Note": safe_note,
            }
        )
        print(f"💾 저장 완료: {event_name} / 타겟={res['Target']} / Unit={res['Unit']}")
        return True

    except Exception as e:
        print(f"❌ Predictions 저장 실패: {e}")
        return False


def run_auto_predictions():
    print("🔥 [토요일 자동 예측 배치] 시작")
    matchups = get_upcoming_matchups()

    if not matchups:
        print("🛑 자동 분석 대상 경기가 없어 종료")
        return

    saved_count = 0
    pass_count = 0
    fail_count = 0

    for idx, m in enumerate(matchups, start=1):
        print("\n" + "=" * 70)
        print(f"[{idx}/{len(matchups)}] 🥊 {m['fighter_a']} vs {m['fighter_b']}")
        print(f"   ↳ 배당률: {m['fighter_a']} {m['odds_a']} / {m['fighter_b']} {m['odds_b']}")

        try:
            A_quant = pred.get_quant_data(m["fighter_a"])
            B_quant = pred.get_quant_data(m["fighter_b"])
            if not A_quant or not B_quant:
                print("❌ 정량 데이터 부족으로 스킵")
                fail_count += 1
                continue

            dify_json = pred.run_dify_extraction(m["fighter_a"], m["fighter_b"])
            if not dify_json:
                print("❌ Dify 추출 실패로 스킵")
                fail_count += 1
                continue

            A_dify = dify_json.get("fighter_A", {})
            B_dify = dify_json.get("fighter_B", {})

            res = pred.calculate_hybrid_model(
                A_quant,
                B_quant,
                A_dify,
                B_dify,
                m["odds_a"],
                m["odds_b"],
            )

            print(
                f"📈 모델 승률: {res['A_name']} {res['A_prob']:.1%} / {res['B_name']} {res['B_prob']:.1%}"
            )
            print(f"🎯 타겟: {res['Target']} | Edge: {res['Edge']:.1%} | Unit: {res['Unit']}")

            if "PASS" in res["Target"]:
                pass_count += 1
                print("⏭️ PASS 경기로 분류되어 저장하지 않음")
                continue

            ok = save_prediction(
                event_name=m["event_name"],
                res=res,
                dify_json=dify_json,
                A_quant=A_quant,
                B_quant=B_quant,
                odds_a=m["odds_a"],
                odds_b=m["odds_b"],
            )
            if ok:
                saved_count += 1
            else:
                fail_count += 1

        except Exception as e:
            print(f"❌ 경기 자동 분석 실패: {e}")
            fail_count += 1
            continue

    print("\n" + "=" * 70)
    print("🏁 [토요일 자동 예측 배치 종료]")
    print(f"✅ 저장 성공: {saved_count}건")
    print(f"⏭️ PASS/보류: {pass_count}건")
    print(f"❌ 실패: {fail_count}건")


if __name__ == "__main__":
    run_auto_predictions()
