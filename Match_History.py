from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from webdriver_manager.chrome import ChromeDriverManager
from bs4 import BeautifulSoup
from pyairtable import Api
import time
import random
import re
import unicodedata
from datetime import datetime

# ==========================================
# ⚠️ [설정] 본인 토큰 확인
# ==========================================
from config import AIRTABLE_TOKEN, AIRTABLE_BASE_ID
FIGHTERS_TABLE_NAME = "Fighters"
HISTORY_TABLE_NAME = "Match_History"

MATCH_LIMIT = 7  # 최근 7경기 수집 제한

# ==========================================
# 🛠️ [데이터 정제 및 퀀트 헬퍼 함수]
# ==========================================
def clean_date(date_text):
    try:
        return datetime.strptime(date_text, "%b. %d, %Y").strftime("%Y-%m-%d")
    except:
        return None


def parse_stat_fraction(stat_str):
    """ '10 of 20' 같은 문자를 정수 10, 20으로 분리. '0 of 0'도 완벽 대응 """
    try:
        nums = re.findall(r"\d+", stat_str)
        if len(nums) >= 2:
            return int(nums[0]), int(nums[1])
        return 0, 0
    except:
        return 0, 0


def get_unified_name(name):
    """Lone'Er Kavanagh 표기 오류 및 동명이인 방어를 위한 유니파이드 압축"""
    if not name:
        return ""
    n = name.lower()
    n = unicodedata.normalize("NFKD", n).encode("ASCII", "ignore").decode("utf-8")
    n = re.sub(r"[^a-z0-9]", "", n)
    return n


def calculate_weight(opp_z_score):
    """기본 공식: W = 1.0 + (Z-Score * 0.2), 0.5~1.5 클램핑"""
    base_w = 1.0 + (opp_z_score * 0.2)
    return max(0.5, min(1.5, base_w))


def get_chrome_driver():
    """
    GitHub Actions / 로컬 겸용 크롬 드라이버
    - 헤드리스
    - 샌드박스 해제
    - /dev/shm 이슈 방어
    """
    options = webdriver.ChromeOptions()
    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64)")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    options.add_experimental_option("useAutomationExtension", False)

    service = Service(ChromeDriverManager().install())
    return webdriver.Chrome(service=service, options=options)


# ==========================================
# 🚀 [메인 로직]
# ==========================================
def main():
    print("🔥 [V5.2 마스터 엔진] 상세 스탯 덮어쓰기 및 제로 스탯 방어 모드 가동...")

    api = Api(AIRTABLE_TOKEN)
    f_table = api.table(AIRTABLE_BASE_ID, FIGHTERS_TABLE_NAME)
    h_table = api.table(AIRTABLE_BASE_ID, HISTORY_TABLE_NAME)

    # 1. Fighter DB 로딩 및 매핑
    print("🧠 전투력(Z-Score) 맵 생성 중...")
    all_fighters = f_table.all()
    fighter_map = {}

    for f in all_fighters:
        fields = f["fields"]
        name = fields.get("Name", "")
        unified_name = get_unified_name(name)

        fighter_map[unified_name] = {
            "id": f["id"],
            "z_slpm": float(fields.get("z_slpm", 0.0)),
            "z_td_avg": float(fields.get("z_td_avg", 0.0)),
            "z_str_def": float(fields.get("z_str_def", 0.0)),
            "z_tdd": float(fields.get("z_tdd", 0.0)),
            "rank": str(fields.get("Current_Ranking", "Unranked")).strip(),
        }

    # 2. 기존 Match History 스캔 (★ 강제 덮어쓰기 로직 ★)
    print("📚 기존 기록 스캔 중...")
    existing_matches = {}

    for record in h_table.iterate():
        for r in record:
            fields = r["fields"]
            f_list = fields.get("Fighter")
            date = fields.get("Date")
            if f_list and date:
                # 이미 상세 스탯이 채워져 있는지 확인
                has_stats = (
                    "Fighter_Sig_Str_Landed" in fields
                    and fields.get("Fighter_Sig_Str_Landed") is not None
                )
                existing_matches[(f_list[0], date)] = {
                    "record_id": r["id"],
                    "has_stats": has_stats,
                }

    driver = None

    try:
        # 3. 브라우저 세팅
        driver = get_chrome_driver()
        driver.get("https://www.google.com")
        time.sleep(2.0)

        main_window = driver.current_window_handle

        total_created = 0
        total_updated = 0

        # 4. 선수별 상세 경기 수집 및 연산 시작
        for idx, fighter in enumerate(all_fighters):
            f_name = fighter["fields"].get("Name", "")
            f_id = fighter["id"]
            f_url = fighter["fields"].get("UFC_Stats_URL")

            if not f_url:
                continue

            print(f"[{idx+1}/{len(all_fighters)}] 🥊 {f_name} 분석 시작...")

            try:
                driver.switch_to.window(main_window)
                driver.get(f_url)
                time.sleep(random.uniform(3.0, 5.0))

                soup = BeautifulSoup(driver.page_source, "html.parser")
                fight_rows = soup.select("tr.b-fight-details__table-row")

                create_records = []
                update_records = []
                saved_count = 0

                for row in fight_rows:
                    if saved_count >= MATCH_LIMIT:
                        break

                    cols = row.select("td")
                    if len(cols) < 10:
                        continue

                    event_p = cols[6].select("p")
                    if len(event_p) < 2:
                        continue

                    date_str = clean_date(event_p[1].get_text(strip=True))
                    match_info = existing_matches.get((f_id, date_str))

                    # 이미 완벽한 스탯이 있으면 스킵
                    if match_info and match_info["has_stats"]:
                        saved_count += 1
                        continue

                    detail_url = row.get("data-link")
                    if not detail_url:
                        continue

                    detail_window = None

                    try:
                        driver.execute_script(f"window.open('{detail_url}', '_blank');")

                        for handle in driver.window_handles:
                            if handle != main_window:
                                detail_window = handle
                                break

                        if not detail_window:
                            raise ValueError("상세 페이지 새 창 핸들 탐지 실패")

                        driver.switch_to.window(detail_window)
                        time.sleep(random.uniform(2.5, 4.0))

                        detail_soup = BeautifulSoup(driver.page_source, "html.parser")
                        fighter_names = [
                            n.get_text(strip=True)
                            for n in detail_soup.select(".b-fight-details__person-name a")
                        ]

                        if len(fighter_names) < 2:
                            raise ValueError("선수 이름 파싱 실패")

                        my_unified = get_unified_name(f_name)
                        f_idx = 0 if my_unified in get_unified_name(fighter_names[0]) else 1
                        opp_idx = 1 - f_idx
                        opp_raw_name = fighter_names[opp_idx]
                        opp_unified = get_unified_name(opp_raw_name)

                        stat_tables = detail_soup.select(".b-fight-details__table")
                        if not stat_tables:
                            raise ValueError("상세 스탯 표 없음")

                        totals_row = None
                        rows_stat = None
                        for tr in stat_tables[0].select("tr"):
                            tds = tr.select("td")
                            if len(tds) >= 10:
                                totals_row = tr
                                rows_stat = tds
                                break

                        if not totals_row or not rows_stat:
                            raise ValueError("유효한 스탯 행 없음")

                        def safe_get_p(col, index):
                            ps = col.select("p")
                            if len(ps) > index:
                                return ps[index].get_text(strip=True)
                            elif len(ps) > 0:
                                return ps[0].get_text(strip=True)
                            return "0"

                        # 1️⃣ 원시 스탯(Raw Stats) 추출
                        opp_sig_str_att_total = safe_get_p(rows_stat[2], opp_idx)
                        opp_sig_landed, opp_sig_att = parse_stat_fraction(opp_sig_str_att_total)

                        opp_td_att_total = safe_get_p(rows_stat[5], opp_idx)
                        opp_td_landed, opp_td_att = parse_stat_fraction(opp_td_att_total)

                        my_sig_str_att_total = safe_get_p(rows_stat[2], f_idx)
                        my_sig_landed, my_sig_att = parse_stat_fraction(my_sig_str_att_total)

                        my_td_att_total = safe_get_p(rows_stat[5], f_idx)
                        my_td_landed, my_td_att = parse_stat_fraction(my_td_att_total)

                        # 2️⃣ 상대방 Z-Score 가져오기 (없으면 -1.5)
                        opp_stats = fighter_map.get(
                            opp_unified,
                            {
                                "z_slpm": -1.5,
                                "z_td_avg": -1.5,
                                "z_str_def": -1.5,
                                "z_tdd": -1.5,
                                "rank": "Unranked",
                            },
                        )

                        # 3️⃣ 가중치(Weight) 산출
                        w_opp_td_acc = calculate_weight(opp_stats["z_td_avg"])
                        w_opp_str_acc = calculate_weight(opp_stats["z_slpm"])
                        w_opp_tdd = calculate_weight(opp_stats["z_tdd"])
                        w_opp_str_def = calculate_weight(opp_stats["z_str_def"])

                        # 4️⃣ True Stat 산출
                        raw_td_defended = opp_td_att - opp_td_landed
                        true_td_defended = raw_td_defended * w_opp_td_acc

                        raw_str_defended = opp_sig_att - opp_sig_landed
                        true_str_defended = raw_str_defended * w_opp_str_acc

                        true_td_landed = my_td_landed * w_opp_tdd
                        true_str_landed = my_sig_landed * w_opp_str_def

                        stat_payload = {
                            "Fighter_Sig_Str_Landed": my_sig_landed,
                            "Fighter_Sig_Str_Att": my_sig_att,
                            "Fighter_TD_Landed": my_td_landed,
                            "Fighter_TD_Att": my_td_att,
                            "Opp_Sig_Str_Att": opp_sig_att,
                            "Fighter_Sig_Str_Abs": opp_sig_landed,
                            "Opp_TD_Att": opp_td_att,
                            "Fighter_TD_Defended": raw_td_defended,
                            "Opp_z_slpm": float(opp_stats["z_slpm"]),
                            "Opp_z_td_avg": float(opp_stats["z_td_avg"]),
                            "True_TD_Defended": float(round(true_td_defended, 3)),
                            "True_Str_Defended": float(round(true_str_defended, 3)),
                            "True_TD_Landed": float(round(true_td_landed, 3)),
                            "True_Str_Landed": float(round(true_str_landed, 3)),
                        }

                        if match_info:
                            update_records.append(
                                {
                                    "id": match_info["record_id"],
                                    "fields": stat_payload,
                                }
                            )
                        else:
                            stat_payload.update(
                                {
                                    "Fighter": [f_id],
                                    "Opponent": opp_raw_name.title(),
                                    "Date": date_str,
                                    "Result": cols[0].get_text(strip=True).upper(),
                                    "Method": cols[7].get_text(separator=" ", strip=True),
                                    "Event": event_p[0].get_text(strip=True),
                                    "Opponent_Tier": opp_stats["rank"],
                                }
                            )
                            create_records.append(stat_payload)

                        saved_count += 1

                    except Exception as inner_e:
                        print(f"      ⚠️ 스탯 파싱 스킵: {inner_e}")

                    finally:
                        # 상세창 닫기
                        try:
                            if detail_window and detail_window in driver.window_handles:
                                driver.switch_to.window(detail_window)
                                driver.close()
                        except Exception:
                            pass

                        try:
                            if main_window in driver.window_handles:
                                driver.switch_to.window(main_window)
                        except Exception:
                            pass

                if create_records:
                    for i in range(0, len(create_records), 10):
                        h_table.batch_create(create_records[i:i+10])
                    print(f"  ✅ +{len(create_records)}건 신규 생성")
                    total_created += len(create_records)

                if update_records:
                    for i in range(0, len(update_records), 10):
                        h_table.batch_update(update_records[i:i+10])
                    print(f"  🔄 +{len(update_records)}건 스탯 덮어쓰기 완료")
                    total_updated += len(update_records)

                if not create_records and not update_records:
                    print("  ✨ 이미 모든 스탯이 완벽함 (업데이트 없음)")

            except Exception as e:
                print(f"  ❌ 메인 페이지 에러: {e}")
                try:
                    if driver and main_window in driver.window_handles:
                        driver.switch_to.window(main_window)
                except Exception:
                    pass

        print(f"\n🏆 V5.2 작업 종료! 신규 {total_created}건, 업데이트 {total_updated}건 처리 완료.")

    finally:
        if driver:
            try:
                driver.quit()
            except Exception:
                pass


if __name__ == "__main__":
    main()