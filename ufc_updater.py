import time
import urllib.parse
import requests
import pandas as pd
import numpy as np
from bs4 import BeautifulSoup
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from webdriver_manager.chrome import ChromeDriverManager
from pyairtable import Api

from config import AIRTABLE_TOKEN, AIRTABLE_BASE_ID
from ufc_utils import *

from fighter_identity import (
    build_fighter_index,
    find_existing_record_by_name,
    resolve_fighter_identity,
    save_review_queue,
    print_exception_report,
)

# ==========================================
# 🔑 Airtable 세팅
# ==========================================
FIGHTERS_TABLE = "Fighters"
HISTORY_TABLE = "Match_History"

api = Api(AIRTABLE_TOKEN)
f_table = api.table(AIRTABLE_BASE_ID, FIGHTERS_TABLE)
h_table = api.table(AIRTABLE_BASE_ID, HISTORY_TABLE)


def get_chrome_driver():
    """
    GitHub Actions/로컬 겸용 크롬 드라이버
    """
    options = webdriver.ChromeOptions()
    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64)")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    options.add_experimental_option("useAutomationExtension", False)

    return webdriver.Chrome(
        service=Service(ChromeDriverManager().install()),
        options=options
    )


def is_ranked_or_champion(ranking_value: str) -> bool:
    if not ranking_value:
        return False

    r = str(ranking_value).strip().lower()
    if r == "champion":
        return True
    if r.isdigit():
        return True
    return False


def is_promotable_to_gatekeeper(ranking_value: str) -> bool:
    """
    Prospect / Unranked / 빈칸만 Gatekeeper 승격 허용
    """
    if not ranking_value:
        return True

    r = str(ranking_value).strip().lower()

    if r in ["prospect", "unranked", ""]:
        return True
    if r == "champion" or r.isdigit():
        return False
    if "gatekeeper" in r:
        return False

    return False


def safe_create_fighter(payload):
    try:
        return f_table.create(payload)
    except Exception as e:
        msg = str(e)
        if "Unknown field name" in msg:
            core = {
                k: v
                for k, v in payload.items()
                if k not in ["Data_Status", "Data_Confidence", "Validation_Note"]
            }
            return f_table.create(core)
        raise


def safe_update_fighter(record_id, payload):
    try:
        return f_table.update(record_id, payload)
    except Exception as e:
        msg = str(e)
        if "Unknown field name" in msg:
            core = {
                k: v
                for k, v in payload.items()
                if k not in ["Data_Status", "Data_Confidence", "Validation_Note"]
            }
            return f_table.update(record_id, core)
        raise


def update_official_rankings(f_table):
    """
    1. UFC 공식 랭킹 스크래핑 및 신규 랭커 추가 + 기존 랭커 업데이트
    """
    print("\n🌐 [Phase 1] 공식 랭킹 동기화 중...")

    driver = get_chrome_driver()
    try:
        driver.get("https://kr.ufc.com/rankings")
        time.sleep(3.0)

        soup = BeautifulSoup(driver.page_source, "html.parser")
        ranked_data = {}

        weight_class_map = {
            "플라이급": "Flyweight",
            "밴텀급": "Bantamweight",
            "페더급": "Featherweight",
            "라이트급": "Lightweight",
            "웰터급": "Welterweight",
            "미들급": "Middleweight",
            "라이트 헤비급": "Light Heavyweight",
            "헤비급": "Heavyweight",
        }

        weight_classes = soup.select(".view-grouping")

        for wc_block in weight_classes:
            wc_header = wc_block.select_one(".view-grouping-header")
            if not wc_header:
                continue

            raw_wc_name = wc_header.get_text(strip=True).replace("Top Rank", "").strip()
            wc_lower = raw_wc_name.lower()

            if "women" in wc_lower or "여성" in wc_lower or "pound" in wc_lower:
                continue

            clean_wc_name = raw_wc_name
            for kr_name in sorted(weight_class_map.keys(), key=len, reverse=True):
                if kr_name in wc_lower:
                    clean_wc_name = weight_class_map[kr_name]
                    break

            champion_tag = wc_block.select_one("#champion-image-name a") or wc_block.select_one("h5 a")
            if champion_tag:
                ranked_data[champion_tag.get_text(strip=True)] = {
                    "weight_class": clean_wc_name,
                    "rank": "Champion",
                }

            for row in wc_block.select("tr"):
                name_tag = row.select_one(".views-field-title a")
                rank_tag = row.select_one(".views-field-weight-class-rank")

                if name_tag and rank_tag:
                    ranked_data[name_tag.get_text(strip=True)] = {
                        "weight_class": clean_wc_name,
                        "rank": rank_tag.get_text(strip=True),
                    }

    finally:
        driver.quit()

    existing_records = f_table.all()
    indexed_records = build_fighter_index(existing_records)

    create_batch = []
    update_count = 0

    for ufc_name, data in ranked_data.items():
        rec = find_existing_record_by_name(ufc_name, indexed_records)

        if rec:
            update_payload = {}

            if str(rec.get("ranking", "")) != str(data["rank"]):
                update_payload["Current_Ranking"] = str(data["rank"])

            if rec.get("weight_class", "") != data["weight_class"]:
                update_payload["Weight_Class"] = data["weight_class"]

            if update_payload:
                safe_update_fighter(rec["id"], update_payload)
                update_count += 1
        else:
            create_batch.append(
                {
                    "Name": ufc_name,
                    "Weight_Class": data["weight_class"],
                    "Current_Ranking": str(data["rank"]),
                    "Data_Status": "Verified",
                    "Data_Confidence": 100,
                    "Validation_Note": "Official UFC rankings source",
                }
            )

    if create_batch:
        for row in create_batch:
            safe_create_fighter(row)
        print(f"✅ 신규 랭커 {len(create_batch)}명 추가 완료.")
    else:
        print("✅ 신규 진입 랭커 없음.")

    print(f"✅ 기존 랭킹/체급 업데이트 {update_count}건 완료.")


def update_gatekeepers_and_missing_stats(f_table, h_table):
    """
    2. 수문장 탐지 + Prospect/Unranked → Gatekeeper 승격 + 누락 스탯 보강
    """
    print("\n🔍 [Phase 2] 수문장 탐지 및 지표 수집...")

    opp_counts = {}
    for page in h_table.iterate():
        for r in page:
            opp_name = str(r["fields"].get("Opponent", "")).strip()
            if opp_name:
                opp_counts[opp_name] = opp_counts.get(opp_name, 0) + 1

    candidates = [name for name, count in opp_counts.items() if count >= 2]
    print(f"✅ Gatekeeper 후보 {len(candidates)}명 감지")

    all_f = f_table.all()
    existing_index = build_fighter_index(all_f)
    review_queue = []

    for cand in candidates:
        matched = find_existing_record_by_name(cand, existing_index)

        if matched:
            current_ranking = matched["ranking"]

            if is_ranked_or_champion(current_ranking):
                continue

            update_payload = {}

            if is_promotable_to_gatekeeper(current_ranking):
                update_payload["Current_Ranking"] = "Gatekeeper"
                print(f"   ➔ Gatekeeper 승격: {matched['name']} ({current_ranking} → Gatekeeper)")

            if not matched.get("url"):
                resolution = resolve_fighter_identity(
                    target_name=cand,
                    expected_weight_class=matched.get("weight_class", ""),
                    manual_overrides={},
                )

                if resolution["status"] == "verified":
                    profile = resolution["profile"]
                    update_payload["UFC_Stats_URL"] = profile.get("UFC_Stats_URL")
                    update_payload["Data_Status"] = "Verified"
                    update_payload["Data_Confidence"] = int(resolution.get("confidence", 0))
                    update_payload["Validation_Note"] = f"Gatekeeper URL resolve | {resolution['note']}"
                    print(f"   ➔ UFC URL 보강: {matched['name']}")
                else:
                    review_queue.append(
                        {
                            "target_name": cand,
                            "expected_weight_class": matched.get("weight_class", ""),
                            "context": f"Gatekeeper candidate / existing={matched['name']}",
                            "reason": resolution["note"],
                            "candidates": [
                                {
                                    "candidate_name": c.get("candidate_name", ""),
                                    "weight_class": c.get("weight_class", ""),
                                    "url": c.get("url", ""),
                                    "score": c.get("score", 0),
                                }
                                for c in resolution.get("candidates", [])
                            ],
                        }
                    )

            if update_payload:
                safe_update_fighter(matched["id"], update_payload)

        else:
            resolution = resolve_fighter_identity(
                target_name=cand,
                expected_weight_class=None,
                manual_overrides={},
            )

            if resolution["status"] == "verified":
                profile = resolution["profile"]

                payload = {
                    "Name": cand.title(),
                    "Current_Ranking": "Gatekeeper",
                    "UFC_Stats_URL": profile.get("UFC_Stats_URL"),
                    "Height": profile.get("Height"),
                    "Reach": profile.get("Reach"),
                    "Age": profile.get("Age"),
                    "SLpM": profile.get("SLpM"),
                    "Str_Acc": profile.get("Str_Acc"),
                    "SApM": profile.get("SApM"),
                    "Str_Def": profile.get("Str_Def"),
                    "TD_Avg": profile.get("TD_Avg"),
                    "TD_Acc": profile.get("TD_Acc"),
                    "TD_Def": profile.get("TD_Def"),
                    "Sub_Avg": profile.get("Sub_Avg"),
                    "Weight_Class": profile.get("Weight_Class"),
                    "UFC_Fights": profile.get("UFC_Fights"),
                    "Data_Status": "Verified",
                    "Data_Confidence": int(resolution.get("confidence", 0)),
                    "Validation_Note": f"New Gatekeeper create | {resolution['note']}",
                }
                safe_create_fighter(payload)
                print(f"   ➔ 신규 Gatekeeper 생성: {cand.title()}")
            else:
                review_queue.append(
                    {
                        "target_name": cand,
                        "expected_weight_class": "",
                        "context": "New gatekeeper candidate",
                        "reason": resolution["note"],
                        "candidates": [
                            {
                                "candidate_name": c.get("candidate_name", ""),
                                "weight_class": c.get("weight_class", ""),
                                "url": c.get("url", ""),
                                "score": c.get("score", 0),
                            }
                            for c in resolution.get("candidates", [])
                        ],
                    }
                )

    # 누락 스탯 보강
    required = [
        "Str_Acc", "Str_Def", "TD_Acc", "TD_Def", "Sub_Avg",
        "Weight_Class", "Age", "Reach", "Height", "UFC_Fights"
    ]

    for f in f_table.all():
        fields = f["fields"]
        target_url = fields.get("UFC_Stats_URL")

        if target_url and not all(fields.get(s) is not None for s in required):
            try:
                headers = {"User-Agent": "Mozilla/5.0"}
                res = requests.get(target_url, headers=headers, timeout=15)
                res.raise_for_status()

                soup = BeautifulSoup(res.text, "html.parser")
                upd = {}
                weight_lbs = ""

                for item in soup.select("li.b-list__box-list-item"):
                    txt = item.get_text(strip=True)

                    if "Height:" in txt:
                        upd["Height"] = convert_height(txt.replace("Height:", ""))
                    if "Weight:" in txt:
                        weight_lbs = txt.replace("Weight:", "").strip()
                    if "Reach:" in txt:
                        upd["Reach"] = convert_reach(txt.replace("Reach:", ""))
                    if "DOB:" in txt:
                        upd["Age"] = calculate_age(txt.replace("DOB:", ""))
                    if "SLpM:" in txt:
                        upd["SLpM"] = clean_float(txt.replace("SLpM:", ""))
                    if "Str. Acc.:" in txt:
                        upd["Str_Acc"] = clean_percent(txt.replace("Str. Acc.:", ""))
                    if "SApM:" in txt:
                        upd["SApM"] = clean_float(txt.replace("SApM:", ""))
                    if "Str. Def" in txt:
                        upd["Str_Def"] = clean_percent(
                            txt.split("Def")[-1].replace(".:", "").replace(":", "").strip()
                        )
                    if "TD Avg.:" in txt:
                        upd["TD_Avg"] = clean_float(txt.replace("TD Avg.:", ""))
                    if "TD Acc.:" in txt:
                        upd["TD_Acc"] = clean_percent(txt.replace("TD Acc.:", ""))
                    if "TD Def.:" in txt:
                        upd["TD_Def"] = clean_percent(txt.replace("TD Def.:", ""))
                    if "Sub. Avg.:" in txt:
                        upd["Sub_Avg"] = clean_float(txt.replace("Sub. Avg.:", ""))

                if not fields.get("Weight_Class"):
                    gc = guess_weight_class(weight_lbs)
                    if gc != "Unknown":
                        upd["Weight_Class"] = gc

                upd["UFC_Fights"] = sum(
                    1 for row in soup.select("tr.b-fight-details__table-row") if row.find("td")
                )

                if upd:
                    safe_update_fighter(f["id"], upd)
                time.sleep(0.5)

            except Exception as e:
                print(f"   ➔ 스탯 보강 실패: {fields.get('Name', 'Unknown')} / {e}")

    save_review_queue(review_queue, "manual_review_queue_updater.json")
    print_exception_report(review_queue, title="Updater 수동 확인 필요 목록")


def recalculate_z_scores(f_table, h_table):
    """
    3. 체급별 Proxy Z-Score 및 Match_History 기반 최종 True Z-Score 업데이트
    - Prospect 제외
    - Unranked 제외
    - Champion / 숫자랭커 / Gatekeeper 기준 평균
    """
    print("\n📊 [Phase 3] Proxy 및 최종 True Z-Score 이중 연산 시작...")

    df_f = pd.DataFrame([{**f["fields"], "record_id": f["id"]} for f in f_table.all()])
    df_h = pd.DataFrame([h["fields"] for h in h_table.all()])

    if df_f.empty:
        print("❌ Fighters 테이블이 비어 있어 Z-Score 계산 중단")
        return

    print(" 🔄 Proxy Z-Score 계산 중...")

    elite_f = df_f[
        df_f["Current_Ranking"].notna()
        & (df_f["Current_Ranking"] != "Unranked")
        & (df_f["Current_Ranking"] != "Prospect")
    ].copy()

    proxy_map = {
        "SLpM": "z_slpm",
        "TD_Avg": "z_td_avg",
        "TD_Def": "z_tdd",
        "Str_Def": "z_str_def",
    }

    for raw, z in proxy_map.items():
        df_f[raw] = pd.to_numeric(df_f.get(raw), errors="coerce")
        elite_f[raw] = pd.to_numeric(elite_f.get(raw), errors="coerce")

        stats = elite_f.groupby("Weight_Class")[raw].agg(m="mean", s="std").reset_index()
        df_f = df_f.merge(stats, on="Weight_Class", how="left")

        df_f[z] = np.where(
            (df_f["s"] > 0),
            (df_f[raw] - df_f["m"]) / df_f["s"],
            0.0
        )

        df_f[z] = df_f[z].round(3).fillna(0.0)
        df_f.drop(columns=["m", "s"], inplace=True)

    print(" 🔄 Match_History 보정 데이터(True Stat) 집계 중...")

    if not df_h.empty and "Fighter" in df_h.columns:
        df_h["f_id"] = df_h["Fighter"].apply(lambda x: x[0] if isinstance(x, list) else x)

        required_cols = [
            "True_Str_Landed",
            "True_TD_Landed",
            "True_TD_Defended",
            "True_Str_Defended",
        ]
        for col in required_cols:
            if col not in df_h.columns:
                df_h[col] = np.nan

        true_agg = df_h.groupby("f_id").agg(
            {
                "True_Str_Landed": "mean",
                "True_TD_Landed": "mean",
                "True_TD_Defended": "mean",
                "True_Str_Defended": "mean",
            }
        ).reset_index()

        df_f = df_f.merge(true_agg, left_on="record_id", right_on="f_id", how="left")

        true_z_map = {
            "True_Str_Landed": "True_z_slpm",
            "True_TD_Landed": "True_z_td_avg",
            "True_TD_Defended": "True_z_tdd",
            "True_Str_Defended": "True_z_str_def",
        }

        for t_raw, t_z in true_z_map.items():
            elite_t = df_f[
                df_f["Current_Ranking"].notna()
                & (df_f["Current_Ranking"] != "Unranked")
                & (df_f["Current_Ranking"] != "Prospect")
            ].copy()

            elite_t[t_raw] = pd.to_numeric(elite_t.get(t_raw), errors="coerce")

            t_stats = elite_t.groupby("Weight_Class")[t_raw].agg(m="mean", s="std").reset_index()
            df_f = df_f.merge(t_stats, on="Weight_Class", how="left", suffixes=("", "_tmp"))

            df_f[t_z] = np.where(
                (df_f["s"] > 0),
                (df_f[t_raw] - df_f["m"]) / df_f["s"],
                0.0
            )

            df_f[t_z] = df_f[t_z].round(3).fillna(0.0)
            df_f.drop(columns=["m", "s"], inplace=True)

    else:
        print(" ⚠️ Match_History가 비어 있거나 Fighter 칼럼이 없어 True Z-Score는 0.0 처리")
        for z_col in ["True_z_slpm", "True_z_td_avg", "True_z_tdd", "True_z_str_def"]:
            df_f[z_col] = 0.0

    update_batch = []
    for _, row in df_f.iterrows():
        payload = {
            "z_slpm": float(row.get("z_slpm", 0.0)),
            "z_td_avg": float(row.get("z_td_avg", 0.0)),
            "z_tdd": float(row.get("z_tdd", 0.0)),
            "z_str_def": float(row.get("z_str_def", 0.0)),
            "True_z_slpm": float(row.get("True_z_slpm", 0.0)),
            "True_z_td_avg": float(row.get("True_z_td_avg", 0.0)),
            "True_z_tdd": float(row.get("True_z_tdd", 0.0)),
            "True_z_str_def": float(row.get("True_z_str_def", 0.0)),
        }
        update_batch.append({"id": row["record_id"], "fields": payload})

    for i in range(0, len(update_batch), 10):
        f_table.batch_update(update_batch[i:i + 10])

    print(f"✅ 총 {len(update_batch)}명 Proxy & 최종 True Z-Score 업데이트 완료!")