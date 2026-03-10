import time
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

# ==========================================
# ⚙️ 정책 상수
# ==========================================
# Gatekeeper는 "자주 보인 선수"가 아니라 "실제로 검증된 중간층"만 남기도록 강화
GATEKEEPER_MIN_UFC_FIGHTS = 5
GATEKEEPER_TOTAL_EXPOSURE_THRESHOLD = 3
GATEKEEPER_BENCHMARK_EXPOSURE_THRESHOLD = 2
GATEKEEPER_RANKED_EXPOSURE_THRESHOLD = 1

# Prospect는 아직 검증 전 단계로 제한
PROSPECT_MAX_UFC_FIGHTS = 5

# benchmark(Z-score 평균/표준편차) 계산 시에만 적용
BENCHMARK_RECENCY_DAYS = 730


def get_chrome_driver():
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
        options=options,
    )


def is_ranked_or_champion(ranking_value: str) -> bool:
    if not ranking_value:
        return False
    r = str(ranking_value).strip().lower()
    return r == "champion" or r.isdigit()


def is_gatekeeper(ranking_value: str) -> bool:
    if not ranking_value:
        return False
    return "gatekeeper" in str(ranking_value).strip().lower()


def safe_int(value, default=0):
    try:
        return int(float(value))
    except Exception:
        return default


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


def names_match(name_a: str, name_b: str) -> bool:
    tokens_a = get_name_tokens(name_a)
    tokens_b = get_name_tokens(name_b)

    if not tokens_a or not tokens_b:
        return False

    if tokens_a == tokens_b:
        return True

    return len(tokens_a.intersection(tokens_b)) >= 2


def get_name_key(name: str):
    tokens = sorted(get_name_tokens(name))
    return tuple(tokens)


def empty_exposure_summary(display_name=""):
    return {
        "display_name": display_name,
        "total_count": 0,
        "benchmark_exposure_count": 0,
        "ranked_exposure_count": 0,
    }


def qualifies_gatekeeper(summary: dict, ufc_fights: int) -> bool:
    """
    강화된 Gatekeeper 기준
    1) UFC 5전 이상
    2) Match_History 상대 이름 총 3회 이상 등장
    3) benchmark(Champion/Ranked/Gatekeeper) 선수와 2회 이상 연결
    4) 그중 최소 1회는 공식 랭커/챔피언과 연결
    """
    return (
        ufc_fights >= GATEKEEPER_MIN_UFC_FIGHTS
        and summary.get("total_count", 0) >= GATEKEEPER_TOTAL_EXPOSURE_THRESHOLD
        and summary.get("benchmark_exposure_count", 0) >= GATEKEEPER_BENCHMARK_EXPOSURE_THRESHOLD
        and summary.get("ranked_exposure_count", 0) >= GATEKEEPER_RANKED_EXPOSURE_THRESHOLD
    )


def qualifies_prospect(summary: dict, ufc_fights: int, current_rank: str) -> bool:
    """
    Prospect 기준
    - 아직 Gatekeeper가 아님
    - UFC 5전 이하
    - benchmark 상대와 최소 1회 연결
    """
    r = str(current_rank or "").strip().lower()

    if ufc_fights <= 0 or ufc_fights > PROSPECT_MAX_UFC_FIGHTS:
        return False

    if summary.get("benchmark_exposure_count", 0) >= 1:
        return True

    # 기존 Prospect는 너무 쉽게 날리지 않도록 완충
    if r == "prospect":
        return True

    return False


def get_target_rank_for_nonranked_fighter(fields: dict, summary: dict) -> str:
    current_rank = str(fields.get("Current_Ranking", "")).strip()
    ufc_fights = safe_int(fields.get("UFC_Fights", 0), 0)

    if qualifies_gatekeeper(summary, ufc_fights):
        return "Gatekeeper"

    if qualifies_prospect(summary, ufc_fights, current_rank):
        return "Prospect"

    return "Unranked"


def build_opponent_exposure_summary(h_table, fighter_rank_by_id: dict):
    """
    Match_History에서 Opponent 기준 노출도를 집계한다.

    핵심:
    - total_count: 상대 이름이 전체 Match_History에서 총 몇 번 등장했는가
    - benchmark_exposure_count: 그 상대가 Champion/Ranked/Gatekeeper 선수의 기록에 몇 번 등장했는가
    - ranked_exposure_count: 그 상대가 Champion/숫자랭커 선수의 기록에 몇 번 등장했는가

    기존처럼 '이름 2번 보였네?' 수준으로 Gatekeeper를 만들지 않기 위해
    연결된 상대의 질까지 같이 본다.
    """
    summary_map = {}

    for page in h_table.iterate():
        for r in page:
            fields = r.get("fields", {})
            opp_name = str(fields.get("Opponent", "")).strip()
            fighter_ref = fields.get("Fighter")

            if not opp_name:
                continue

            key = get_name_key(opp_name)
            if not key:
                continue

            if key not in summary_map:
                summary_map[key] = empty_exposure_summary(display_name=opp_name.title())

            summary_map[key]["total_count"] += 1

            fighter_id = None
            if isinstance(fighter_ref, list) and fighter_ref:
                fighter_id = fighter_ref[0]
            elif isinstance(fighter_ref, str):
                fighter_id = fighter_ref

            source_rank = fighter_rank_by_id.get(fighter_id, "")

            if is_ranked_or_champion(source_rank) or is_gatekeeper(source_rank):
                summary_map[key]["benchmark_exposure_count"] += 1

            if is_ranked_or_champion(source_rank):
                summary_map[key]["ranked_exposure_count"] += 1

    return summary_map


def scrape_official_rankings():
    """
    공식 UFC 랭킹을 체급별 slot map으로 스크랩
    반환 형식:
    {
      "Flyweight": {
         "Champion": "Brandon Royval",
         "1": "...",
         ...
      },
      ...
    }
    """
    print("\n🌐 [Phase 1] 공식 랭킹 동기화 중...")

    driver = get_chrome_driver()
    try:
        driver.get("https://kr.ufc.com/rankings")
        time.sleep(3.0)

        soup = BeautifulSoup(driver.page_source, "html.parser")
        ranked_by_class = {}
        ranking_conflicts = []

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

            ranked_by_class.setdefault(clean_wc_name, {})

            champion_tag = wc_block.select_one("#champion-image-name a") or wc_block.select_one("h5 a")
            if champion_tag:
                champ_name = champion_tag.get_text(strip=True)
                if "Champion" not in ranked_by_class[clean_wc_name]:
                    ranked_by_class[clean_wc_name]["Champion"] = champ_name
                elif ranked_by_class[clean_wc_name]["Champion"] != champ_name:
                    ranking_conflicts.append(
                        {
                            "weight_class": clean_wc_name,
                            "slot": "Champion",
                            "existing": ranked_by_class[clean_wc_name]["Champion"],
                            "incoming": champ_name,
                            "reason": "duplicate champion slot on source page",
                        }
                    )

            for row in wc_block.select("tr"):
                name_tag = row.select_one(".views-field-title a")
                rank_tag = row.select_one(".views-field-weight-class-rank")

                if not name_tag or not rank_tag:
                    continue

                fighter_name = name_tag.get_text(strip=True)
                rank_text = rank_tag.get_text(strip=True)

                if not str(rank_text).isdigit():
                    continue

                slot = str(rank_text)

                if slot not in ranked_by_class[clean_wc_name]:
                    ranked_by_class[clean_wc_name][slot] = fighter_name
                else:
                    if ranked_by_class[clean_wc_name][slot] != fighter_name:
                        ranking_conflicts.append(
                            {
                                "weight_class": clean_wc_name,
                                "slot": slot,
                                "existing": ranked_by_class[clean_wc_name][slot],
                                "incoming": fighter_name,
                                "reason": "duplicate numeric rank slot on source page",
                            }
                        )

        return ranked_by_class, ranking_conflicts

    finally:
        driver.quit()


def update_official_rankings(f_table):
    """
    공식 랭킹 동기화

    핵심:
    1) 체급별 slot map 확보
    2) 기존 챔피언/숫자 랭커 중 이번 공식 랭킹에 없는 선수는 먼저 Unranked로 내림
       (이후 Gatekeeper 여부는 Phase 2에서 다시 엄격하게 판정)
    3) 새 공식 랭킹을 단 한 명씩 배정
    """
    ranked_by_class, ranking_conflicts = scrape_official_rankings()

    existing_records = f_table.all()

    print("🧹 기존 랭킹 슬롯 정리 중...")

    demoted_count = 0
    for rec in existing_records:
        fields = rec.get("fields", {})
        current_rank = str(fields.get("Current_Ranking", "")).strip()
        current_wc = fields.get("Weight_Class", "")
        current_name = fields.get("Name", "")

        if not is_ranked_or_champion(current_rank):
            continue

        if current_wc not in ranked_by_class:
            continue

        official_names_this_wc = list(ranked_by_class[current_wc].values())
        still_ranked = any(names_match(current_name, official_name) for official_name in official_names_this_wc)

        if not still_ranked:
            safe_update_fighter(
                rec["id"],
                {
                    "Current_Ranking": "Unranked",
                    "Validation_Note": "Official ranking refresh demotion -> Unranked (Gatekeeper recheck in Phase 2)",
                },
            )
            demoted_count += 1

    print(f"✅ 기존 랭커 강등 처리 {demoted_count}건 완료")

    print("📌 새 공식 랭킹 배정 중...")

    created_count = 0
    updated_count = 0

    existing_records = f_table.all()
    indexed_records = build_fighter_index(existing_records)

    for weight_class, slot_map in ranked_by_class.items():
        for slot, fighter_name in slot_map.items():
            rec = find_existing_record_by_name(fighter_name, indexed_records)

            if rec:
                update_payload = {}

                if rec.get("ranking", "") != slot:
                    update_payload["Current_Ranking"] = slot

                if rec.get("weight_class", "") != weight_class:
                    update_payload["Weight_Class"] = weight_class

                if update_payload:
                    safe_update_fighter(rec["id"], update_payload)
                    updated_count += 1

            else:
                payload = {
                    "Name": fighter_name,
                    "Weight_Class": weight_class,
                    "Current_Ranking": slot,
                    "Data_Status": "Verified",
                    "Data_Confidence": 100,
                    "Validation_Note": "Official UFC rankings source",
                }
                safe_create_fighter(payload)
                created_count += 1

    print(f"✅ 신규 랭커 생성 {created_count}건")
    print(f"✅ 기존 랭커 갱신 {updated_count}건")

    if ranking_conflicts:
        review_items = []
        for c in ranking_conflicts:
            review_items.append(
                {
                    "target_name": f"{c['weight_class']} #{c['slot']}",
                    "expected_weight_class": c["weight_class"],
                    "context": f"existing={c['existing']} / incoming={c['incoming']}",
                    "reason": c["reason"],
                    "candidates": [],
                }
            )
        save_review_queue(review_items, "manual_review_queue_rankings.json")
        print_exception_report(review_items, title="랭킹 소스 충돌 보고서")
    else:
        print("✅ 랭킹 소스 충돌 없음")


def update_gatekeepers_and_missing_stats(f_table, h_table):
    """
    Gatekeeper / Prospect 재분류 전면 수정판

    기존 문제:
    - Opponent 2회 등장 + UFC 3전 정도면 너무 쉽게 Gatekeeper가 됨
    - 그래서 Prospect 풀이 거의 사라지고 Gatekeeper가 비정상적으로 폭증함

    수정 후:
    1) Match_History 노출도(summary)를 먼저 만든다
    2) 기존 non-ranked 선수 전원을 다시 판정한다
       - Gatekeeper
       - Prospect
       - Unranked
    3) DB에 없는 이름은 정말 강한 기준을 만족할 때만 신규 Gatekeeper 생성
    """
    print("\n🔍 [Phase 2] Gatekeeper / Prospect 재분류 중...")

    all_fighters = f_table.all()
    fighter_rank_by_id = {
        r["id"]: str(r.get("fields", {}).get("Current_Ranking", "")).strip()
        for r in all_fighters
    }

    exposure_map = build_opponent_exposure_summary(h_table, fighter_rank_by_id)
    print(f"✅ Match_History 상대 노출 요약 {len(exposure_map)}명 집계 완료")

    existing_index = build_fighter_index(all_fighters)
    review_queue = []

    gatekeeper_promotions = 0
    gatekeeper_creations = 0
    gatekeeper_demotions = 0
    prospect_promotions = 0
    prospect_demotions = 0

    # ------------------------------------------
    # A. 기존 non-ranked 선수 전원 재분류
    # ------------------------------------------
    print("🧹 기존 non-ranked 선수 재분류 중...")

    for rec in all_fighters:
        fields = rec.get("fields", {})
        current_rank = str(fields.get("Current_Ranking", "")).strip()
        current_name = fields.get("Name", "")

        if is_ranked_or_champion(current_rank):
            continue

        name_key = get_name_key(current_name)
        summary = exposure_map.get(name_key, empty_exposure_summary(display_name=current_name))
        target_rank = get_target_rank_for_nonranked_fighter(fields, summary)

        update_payload = {}

        if current_rank != target_rank:
            update_payload["Current_Ranking"] = target_rank

            note = (
                f"Reclassified | total={summary['total_count']} | "
                f"benchmark={summary['benchmark_exposure_count']} | "
                f"ranked={summary['ranked_exposure_count']} | "
                f"UFC_Fights={safe_int(fields.get('UFC_Fights', 0), 0)}"
            )
            update_payload["Validation_Note"] = note

            if current_rank == "Gatekeeper" and target_rank != "Gatekeeper":
                gatekeeper_demotions += 1

            if current_rank != "Gatekeeper" and target_rank == "Gatekeeper":
                gatekeeper_promotions += 1

            if current_rank != "Prospect" and target_rank == "Prospect":
                prospect_promotions += 1

            if current_rank == "Prospect" and target_rank != "Prospect":
                prospect_demotions += 1

        # Gatekeeper로 남는/올라가는 선수는 URL 보강 시도
        if target_rank == "Gatekeeper" and not fields.get("UFC_Stats_URL"):
            resolution = resolve_fighter_identity(
                target_name=current_name,
                expected_weight_class=fields.get("Weight_Class", ""),
                manual_overrides={},
            )

            if resolution["status"] == "verified":
                profile = resolution["profile"]
                update_payload["UFC_Stats_URL"] = profile.get("UFC_Stats_URL")
                update_payload["Data_Status"] = "Verified"
                update_payload["Data_Confidence"] = int(resolution.get("confidence", 0))
            else:
                review_queue.append(
                    {
                        "target_name": current_name,
                        "expected_weight_class": fields.get("Weight_Class", ""),
                        "context": f"Existing fighter / target_rank={target_rank}",
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
            safe_update_fighter(rec["id"], update_payload)

    # ------------------------------------------
    # B. DB에 없는 이름은 정말 강한 기준일 때만 신규 Gatekeeper 생성
    # ------------------------------------------
    print("➕ 신규 Gatekeeper 생성 후보 엄격 검증 중...")

    for key, summary in exposure_map.items():
        if not qualifies_gatekeeper(summary, GATEKEEPER_MIN_UFC_FIGHTS):
            # 이 줄은 "대충 자주 보인 선수"를 후보에서 미리 걸러내기 위한 빠른 차단이 아님
            # 아래에서 진짜 UFC_Fights를 profile 기준으로 다시 확인해야 하므로
            # 여기서는 넘어가지 않는다.
            pass

        cand = summary["display_name"]
        matched = find_existing_record_by_name(cand, existing_index)

        if matched:
            continue

        # 이름은 Match_History에서 많이 보였지만 DB에는 없는 선수
        # → identity resolve 후 profile의 UFC_Fights로 최종 판정
        resolution = resolve_fighter_identity(
            target_name=cand,
            expected_weight_class=None,
            manual_overrides={},
        )

        if resolution["status"] == "verified":
            profile = resolution["profile"]
            ufc_fights = safe_int(profile.get("UFC_Fights", 0), 0)

            if qualifies_gatekeeper(summary, ufc_fights):
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
                    "Validation_Note": (
                        f"New Gatekeeper create | total={summary['total_count']} | "
                        f"benchmark={summary['benchmark_exposure_count']} | "
                        f"ranked={summary['ranked_exposure_count']} | "
                        f"UFC_Fights={ufc_fights}"
                    ),
                }
                safe_create_fighter(payload)
                gatekeeper_creations += 1
                print(
                    f"   ➔ 신규 Gatekeeper 생성: {cand.title()} "
                    f"(total={summary['total_count']}, benchmark={summary['benchmark_exposure_count']}, "
                    f"ranked={summary['ranked_exposure_count']}, UFC_Fights={ufc_fights})"
                )
        else:
            # 정말 유력한 후보만 review queue로 남긴다
            # 너무 많은 쓰레기 후보가 큐를 오염시키지 않도록 필터
            rough_total = summary.get("total_count", 0)
            rough_benchmark = summary.get("benchmark_exposure_count", 0)
            rough_ranked = summary.get("ranked_exposure_count", 0)

            if (
                rough_total >= GATEKEEPER_TOTAL_EXPOSURE_THRESHOLD
                and rough_benchmark >= GATEKEEPER_BENCHMARK_EXPOSURE_THRESHOLD
                and rough_ranked >= GATEKEEPER_RANKED_EXPOSURE_THRESHOLD
            ):
                review_queue.append(
                    {
                        "target_name": cand,
                        "expected_weight_class": "",
                        "context": "New strict gatekeeper candidate",
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

    print(f"✅ Gatekeeper 신규 승격 {gatekeeper_promotions}건")
    print(f"✅ Gatekeeper 신규 생성 {gatekeeper_creations}건")
    print(f"✅ Gatekeeper 강등 {gatekeeper_demotions}건")
    print(f"✅ Prospect 승격 {prospect_promotions}건")
    print(f"✅ Prospect 강등 {prospect_demotions}건")

    # ------------------------------------------
    # C. 누락 스탯 보강
    # ------------------------------------------
    print("🧩 누락 스탯 보강 중...")

    required = [
        "Str_Acc",
        "Str_Def",
        "TD_Acc",
        "TD_Def",
        "Sub_Avg",
        "Weight_Class",
        "Age",
        "Reach",
        "Height",
        "UFC_Fights",
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
    benchmark는 Champion + 숫자랭커 + Gatekeeper
    Prospect / Unranked는 제외

    단, benchmark 평균/표준편차 계산에서만
    최근 경기일이 730일 초과한 선수는 제외한다.
    (DB 삭제/강등 아님)
    """
    print("\n📊 [Phase 3] Proxy 및 최종 True Z-Score 이중 연산 시작...")

    df_f = pd.DataFrame([{**f["fields"], "record_id": f["id"]} for f in f_table.all()])
    df_h = pd.DataFrame([h["fields"] for h in h_table.all()])

    if df_f.empty:
        print("❌ Fighters 테이블이 비어 있어 Z-Score 계산 중단")
        return

    # ------------------------------------------
    # 최근 경기일 계산 (Match_History 기준)
    # ------------------------------------------
    if not df_h.empty and "Fighter" in df_h.columns and "Date" in df_h.columns:
        df_h["f_id"] = df_h["Fighter"].apply(lambda x: x[0] if isinstance(x, list) and x else x)
        df_h["Date_dt"] = pd.to_datetime(df_h["Date"], errors="coerce")

        last_fight_df = (
            df_h.groupby("f_id", as_index=False)["Date_dt"]
            .max()
            .rename(columns={"Date_dt": "Last_Fight_Date"})
        )

        df_f = df_f.merge(last_fight_df, left_on="record_id", right_on="f_id", how="left")
    else:
        df_f["Last_Fight_Date"] = pd.NaT

    today = pd.Timestamp.utcnow().tz_localize(None).normalize()
    df_f["Days_Since_Last_Fight"] = np.where(
        df_f["Last_Fight_Date"].notna(),
        (today - pd.to_datetime(df_f["Last_Fight_Date"], errors="coerce")).dt.days,
        np.nan,
    )

    df_f["Exclude_From_Benchmark"] = (
        df_f["Last_Fight_Date"].notna()
        & (pd.to_numeric(df_f["Days_Since_Last_Fight"], errors="coerce") > BENCHMARK_RECENCY_DAYS)
    )

    print(" 🔄 Proxy Z-Score 계산 중...")

    elite_mask = (
        df_f["Current_Ranking"].notna()
        & (df_f["Current_Ranking"] != "Unranked")
        & (df_f["Current_Ranking"] != "Prospect")
    )

    benchmark_mask = elite_mask & (~df_f["Exclude_From_Benchmark"])
    elite_f = df_f[benchmark_mask].copy()

    if elite_f.empty:
        print(" ⚠️ 최근 활동 benchmark가 비어 있어 비활동 필터 없이 fallback")
        elite_f = df_f[elite_mask].copy()

    print(
        f" ✅ benchmark 모수: {len(elite_f)}명 "
        f"(비활동 제외 {int(df_f['Exclude_From_Benchmark'].sum())}명)"
    )

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
        df_h["f_id"] = df_h["Fighter"].apply(lambda x: x[0] if isinstance(x, list) and x else x)

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

        active_elite_t = df_f[benchmark_mask].copy()
        if active_elite_t.empty:
            active_elite_t = df_f[elite_mask].copy()

        for t_raw, t_z in true_z_map.items():
            active_elite_t[t_raw] = pd.to_numeric(active_elite_t.get(t_raw), errors="coerce")

            t_stats = active_elite_t.groupby("Weight_Class")[t_raw].agg(m="mean", s="std").reset_index()
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


def main():
    print("🔥 [마스터 파이프라인] UFC 데이터 통합 업데이트 시작...")

    update_official_rankings(f_table)
    update_gatekeepers_and_missing_stats(f_table, h_table)
    recalculate_z_scores(f_table, h_table)

    print("\n🏆 모든 데이터 파이프라인 실행 완료! 완벽하게 최신화되었습니다.")


if __name__ == "__main__":
    main()