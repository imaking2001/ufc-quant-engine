import requests
import re
import unicodedata
from datetime import datetime, timedelta
from bs4 import BeautifulSoup
from pyairtable import Api

from config import AIRTABLE_TOKEN, AIRTABLE_BASE_ID

FIGHTERS_TABLE = "Fighters"
PREDICTIONS_TABLE = "Predictions"

LOOKBACK_DAYS = 10  # 이번 주/직전 며칠 범위 정산용

api = Api(AIRTABLE_TOKEN)
f_table = api.table(AIRTABLE_BASE_ID, FIGHTERS_TABLE)
p_table = api.table(AIRTABLE_BASE_ID, PREDICTIONS_TABLE)

def get_unified_name(name):
    if not name:
        return ""
    n = name.lower()
    n = unicodedata.normalize("NFKD", n).encode("ASCII", "ignore").decode("utf-8")
    n = re.sub(r"[^a-z0-9]", "", n)
    return n

def clean_date(date_text):
    try:
        return datetime.strptime(date_text, "%b. %d, %Y").strftime("%Y-%m-%d")
    except Exception:
        return None

def safe_float(value, default=0.0):
    try:
        return float(value)
    except Exception:
        return default

def get_fighter_map():
    fighter_map = {}
    all_fighters = f_table.all()

    for f in all_fighters:
        fields = f.get("fields", {})
        fighter_map[f["id"]] = {
            "name": fields.get("Name", ""),
            "url": fields.get("UFC_Stats_URL", ""),
        }

    return fighter_map

def get_target_predictions():
    records = p_table.all()
    targets = []

    today = datetime.utcnow().date()
    min_date = today - timedelta(days=LOOKBACK_DAYS)

    for r in records:
        fields = r.get("fields", {})

        if fields.get("Actual_Winner"):
            continue

        event_date = fields.get("Event_Date")
        if event_date:
            try:
                parsed = datetime.strptime(event_date, "%Y-%m-%d").date()
                if parsed > today:
                    continue
                if parsed < min_date:
                    continue
            except Exception:
                pass

        targets.append(r)

    return targets

def parse_result_code(raw_text, method_text=""):
    t = str(raw_text).strip().upper()
    m = str(method_text).strip().lower()

    if t == "W":
        return "W"
    if t == "L":
        return "L"
    if t == "D":
        return "D"
    if t in ["NC", "N/C"]:
        return "NC"

    if "draw" in m:
        return "D"
    if "no contest" in m or "nc" == m:
        return "NC"

    return None

def extract_result_from_fighter_page(fighter_name, fighter_url, opponent_name, expected_event_name="", expected_event_date=None):
    if not fighter_url:
        return None

    headers = {"User-Agent": "Mozilla/5.0"}

    try:
        res = requests.get(fighter_url, headers=headers, timeout=20)
        res.raise_for_status()
        soup = BeautifulSoup(res.text, "html.parser")

        target_opp = get_unified_name(opponent_name)
        expected_event = get_unified_name(expected_event_name) if expected_event_name else ""

        rows = soup.select("tr.b-fight-details__table-row")
        candidates = []

        for row in rows:
            cols = row.select("td")
            if len(cols) < 8:
                continue

            fighter_links = row.select("a[href*='fighter-details']")
            names = [a.get_text(strip=True) for a in fighter_links if a.get_text(strip=True)]
            if len(names) < 2:
                continue

            opp_guess = None
            for n in names:
                if get_unified_name(n) != get_unified_name(fighter_name):
                    opp_guess = n
                    break

            if not opp_guess:
                continue

            if get_unified_name(opp_guess) != target_opp:
                continue

            event_ps = cols[6].select("p")
            event_name = event_ps[0].get_text(strip=True) if len(event_ps) >= 1 else ""
            event_date = clean_date(event_ps[1].get_text(strip=True)) if len(event_ps) >= 2 else None

            result_raw = cols[0].get_text(strip=True)
            method_text = cols[7].get_text(separator=" ", strip=True)

            score = 50

            if expected_event and get_unified_name(event_name) == expected_event:
                score += 40

            if expected_event_date and event_date == expected_event_date:
                score += 40

            candidates.append(
                {
                    "score": score,
                    "event_name": event_name,
                    "event_date": event_date,
                    "result_code": parse_result_code(result_raw, method_text),
                    "method": method_text,
                }
            )

        if not candidates:
            return None

        candidates.sort(key=lambda x: x["score"], reverse=True)
        return candidates[0]

    except Exception as e:
        print(f"⚠️ 결과 스크랩 실패: {fighter_name} / {opponent_name} / {e}")
        return None

def resolve_actual_result(main_name, main_url, opponent_name, opponent_url, event_name="", event_date=None):
    result = extract_result_from_fighter_page(
        fighter_name=main_name,
        fighter_url=main_url,
        opponent_name=opponent_name,
        expected_event_name=event_name,
        expected_event_date=event_date,
    )

    if result:
        code = result["result_code"]
        if code == "W":
            return {"Actual_Winner": main_name, "Match_Result": "Win"}
        elif code == "L":
            return {"Actual_Winner": opponent_name, "Match_Result": "Loss"}
        elif code == "D":
            return {"Actual_Winner": "Draw", "Match_Result": "Draw"}
        elif code == "NC":
            return {"Actual_Winner": "NC", "Match_Result": "NC"}

    result = extract_result_from_fighter_page(
        fighter_name=opponent_name,
        fighter_url=opponent_url,
        opponent_name=main_name,
        expected_event_name=event_name,
        expected_event_date=event_date,
    )

    if result:
        code = result["result_code"]
        if code == "W":
            return {"Actual_Winner": opponent_name, "Match_Result": "Loss"}
        elif code == "L":
            return {"Actual_Winner": main_name, "Match_Result": "Win"}
        elif code == "D":
            return {"Actual_Winner": "Draw", "Match_Result": "Draw"}
        elif code == "NC":
            return {"Actual_Winner": "NC", "Match_Result": "NC"}

    return None

def calc_pnl(match_result, odds, unit):
    odds = safe_float(odds, 0.0)
    unit = safe_float(unit, 0.0)

    if match_result == "Win":
        return round(unit * (odds - 1), 2)
    elif match_result == "Loss":
        return round(-unit, 2)
    elif match_result in ["Draw", "NC"]:
        return 0.0
    return 0.0

def main():
    print("💰 [Sunday ROI Settlement Bot] 시작")

    fighter_map = get_fighter_map()
    targets = get_target_predictions()

    if not targets:
        print("ℹ️ 정산 대상 Predictions 레코드 없음")
        return

    print(f"🎯 정산 대상 레코드 수: {len(targets)}건")

    updated = 0
    skipped = 0
    failed = 0

    for idx, record in enumerate(targets, start=1):
        fields = record.get("fields", {})
        record_id = record["id"]

        main_ids = fields.get("Main_Fighter", [])
        opp_ids = fields.get("Opponent", [])

        if not main_ids or not opp_ids:
            print(f"[{idx}] ⚠️ 스킵: linked fighter 없음")
            skipped += 1
            continue

        main_id = main_ids[0]
        opp_id = opp_ids[0]

        main_f = fighter_map.get(main_id)
        opp_f = fighter_map.get(opp_id)

        if not main_f or not opp_f:
            print(f"[{idx}] ⚠️ 스킵: Fighters DB 매핑 실패")
            skipped += 1
            continue

        main_name = main_f["name"]
        main_url = main_f["url"]
        opp_name = opp_f["name"]
        opp_url = opp_f["url"]

        event_name = fields.get("Event_Name", "")
        event_date = fields.get("Event_Date", None)

        print(f"\n[{idx}/{len(targets)}] 🥊 {main_name} vs {opp_name}")
        print(f"   ↳ 이벤트: {event_name}")

        result = resolve_actual_result(
            main_name=main_name,
            main_url=main_url,
            opponent_name=opp_name,
            opponent_url=opp_url,
            event_name=event_name,
            event_date=event_date,
        )

        if not result:
            print("   ↳ ⏭️ 아직 결과 미확정 또는 스크랩 실패")
            skipped += 1
            continue

        market_odds = safe_float(fields.get("Market_Odds"), 0.0)
        bet_unit = safe_float(fields.get("Bet_Amount_Unit"), 0.0)

        pnl = calc_pnl(result["Match_Result"], market_odds, bet_unit)

        payload = {
            "Actual_Winner": result["Actual_Winner"],
            "Match_Result": result["Match_Result"],
            "PnL": round(pnl, 2),
        }

        try:
            p_table.update(record_id, payload)
            print(
                f"   ↳ ✅ 정산 완료 | Actual_Winner={payload['Actual_Winner']} "
                f"/ Match_Result={payload['Match_Result']} / PnL={payload['PnL']}"
            )
            updated += 1
        except Exception as e:
            print(f"   ↳ ❌ Airtable 업데이트 실패: {e}")
            failed += 1

    print("\n" + "=" * 70)
    print("🏁 [Sunday ROI Settlement Bot 종료]")
    print(f"✅ 정산 성공: {updated}건")
    print(f"⏭️ 보류/미확정: {skipped}건")
    print(f"❌ 실패: {failed}건")

if __name__ == "__main__":
    main()