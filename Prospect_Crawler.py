import time
import requests
import urllib.parse
import pandas as pd
import numpy as np
from bs4 import BeautifulSoup
from pyairtable import Api

# 🛠️ 유틸 함수들 (이름 정제, 키/리치 변환, 체급 추정 등)
from ufc_utils import *

# 🔐 보안용 설정 파일 (.env를 통해 실제 키를 읽어옴)
from config import AIRTABLE_TOKEN, AIRTABLE_BASE_ID

# ==========================================
# 🔑 1. Airtable 세팅
# ==========================================
FIGHTERS_TABLE = "Fighters"
HISTORY_TABLE = "Match_History"

api = Api(AIRTABLE_TOKEN)
f_table = api.table(AIRTABLE_BASE_ID, FIGHTERS_TABLE)
h_table = api.table(AIRTABLE_BASE_ID, HISTORY_TABLE)

# ==========================================
# 🎯 2. 유망주 타깃 명단
# ==========================================
TARGET_FIGHTERS = [
    "Michael Johnson",
    "Jesus Santos Aguilar",
    "Nyamjargal Tumendemberel",
    "Raul Rosas Jr"
]


def search_fighter_url(name):
    """
    UFC 검색창의 한계를 뚫는 우회 검색 로직
    - 전체 이름 대신 '성(last name)' 기준으로 넓게 검색
    - 이름 토큰 비교로 실제 타깃만 골라냄
    """
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
        }

        name_parts = name.split()
        search_keyword = name_parts[-1]

        # Raul Rosas Jr 같은 케이스 대응
        if search_keyword.lower() in ["jr", "jr.", "sr", "sr."] and len(name_parts) > 1:
            search_keyword = name_parts[-2]

        print(f"      ➔ [디버그] '{search_keyword}' 키워드로 UFC 서버 우회 스캔 중...")

        res = requests.get(
            f"http://ufcstats.com/statistics/fighters/search?query={urllib.parse.quote(search_keyword)}",
            headers=headers,
            timeout=15
        )
        res.raise_for_status()

        soup = BeautifulSoup(res.text, "html.parser")
        rows = soup.select("tr.b-statistics__table-row")
        target_tokens = get_name_tokens(name)

        for row in rows:
            cols = row.select("td")

            # 실제 데이터가 있는 행만 처리
            if len(cols) > 1 and cols[0].find("a"):
                found = cols[0].get_text(strip=True) + " " + cols[1].get_text(strip=True)
                found_tokens = get_name_tokens(found)

                # 이름이 같거나 토큰 교집합이 충분하면 같은 인물로 간주
                if target_tokens == found_tokens or len(target_tokens.intersection(found_tokens)) >= 2:
                    return cols[0].find("a")["href"]

    except requests.exceptions.Timeout:
        print("      ➔ URL 스캔 에러: UFC 서버 응답 시간이 너무 길어 timeout 발생")
    except requests.exceptions.RequestException as e:
        print(f"      ➔ URL 스캔 네트워크/HTTP 에러: {e}")
    except Exception as e:
        print(f"      ➔ URL 스캔 중 예상치 못한 에러: {e}")

    return None


def add_target_prospects():
    """
    타깃 유망주를 UFC Stats에서 찾아 Airtable Fighters 테이블에 적재
    이미 있으면 패스
    """
    print("🔄 에어테이블 기존 명단 확인 중...")

    try:
        existing_records = f_table.all()
    except Exception as e:
        print(f"❌ Airtable 기존 데이터 조회 실패: {e}")
        print("   ➔ Airtable 토큰, Base ID, Fighters 테이블명 확인 필요")
        return False

    existing_tokens_list = [
        get_name_tokens(r["fields"].get("Name", ""))
        for r in existing_records
    ]

    print(f"🔫 [유망주 스나이퍼 가동] 총 {len(TARGET_FIGHTERS)}명의 타깃 추적 시작...")

    for name in TARGET_FIGHTERS:
        print(f"\n🔍 타깃: {name}")
        target_tokens = get_name_tokens(name)

        if target_tokens in existing_tokens_list:
            print("   ➔ ⚠️ 이미 에어테이블에 존재. 패스!")
            continue

        url = search_fighter_url(name)
        if not url:
            print("   ➔ ❌ URL 찾기 실패. UFC 서버에 해당 선수가 없거나 검색 우회가 실패함.")
            continue

        print(f"   ➔ 🔗 URL 확보 완료: {url}")

        try:
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
            }
            res = requests.get(url, headers=headers, timeout=15)
            res.raise_for_status()

            soup = BeautifulSoup(res.text, "html.parser")

            upd = {
                "Name": name.title(),
                "Current_Ranking": "Prospect",
                "UFC_Stats_URL": url
            }

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

            # 체급이 비어 있으면 weight 기반으로 추정
            if not upd.get("Weight_Class"):
                gc = guess_weight_class(weight_lbs)
                if gc != "Unknown":
                    upd["Weight_Class"] = gc

            # 경기 수 카운트
            upd["UFC_Fights"] = sum(
                1 for row in soup.select("tr.b-fight-details__table-row") if row.find("td")
            )

            f_table.create(upd)
            print(f"   ➔ ✅ [유망주 적재 완료] {name} (체급: {upd.get('Weight_Class', '확인불가')})")

            existing_tokens_list.append(target_tokens)

        except requests.exceptions.Timeout:
            print("   ➔ ❌ 크롤링 에러: UFC Stats 페이지 응답 timeout")
        except requests.exceptions.RequestException as e:
            print(f"   ➔ ❌ 크롤링 네트워크/HTTP 에러: {e}")
        except Exception as e:
            print(f"   ➔ ❌ 크롤링/업로드 에러: {e}")

        # 서버 과부하 방지
        time.sleep(1)

    print("\n🏁 유망주 적재 단계 완료")
    return True


def update_z_scores_only():
    """
    Fighters + Match_History를 바탕으로
    1) Proxy z-score
    2) True z-score
    를 계산해 Airtable Fighters 테이블에 일괄 반영
    """
    print("\n📊 [Z-Score 업데이트] Proxy 및 최종 True Z-Score 계산 시작...")

    try:
        fighters_raw = f_table.all()
        history_raw = h_table.all()
    except Exception as e:
        print(f"❌ Airtable 데이터 불러오기 실패: {e}")
        return False

    df_f = pd.DataFrame([{**f["fields"], "record_id": f["id"]} for f in fighters_raw])
    df_h = pd.DataFrame([h["fields"] for h in history_raw])

    if df_f.empty:
        print("❌ Fighters 테이블이 비어 있음. 계산 중단.")
        return False

    # 계산에 필요한 칼럼이 없으면 기본 생성
    required_fighter_cols = ["Current_Ranking", "Weight_Class", "SLpM", "TD_Avg", "TD_Def", "Str_Def"]
    for col in required_fighter_cols:
        if col not in df_f.columns:
            df_f[col] = np.nan

    # ==========================================
    # PART A. Proxy Z-Score 계산
    # ==========================================
    print(" 🔄 Proxy Z-Score 계산 중...")

    # 랭커 평균 계산 시 Unranked / Prospect는 제외
    elite_f = df_f[
        df_f["Current_Ranking"].notna() &
        (df_f["Current_Ranking"] != "Unranked") &
        (df_f["Current_Ranking"] != "Prospect")
    ].copy()

    proxy_map = {
        "SLpM": "z_slpm",
        "TD_Avg": "z_td_avg",
        "TD_Def": "z_tdd",
        "Str_Def": "z_str_def"
    }

    for raw_col, z_col in proxy_map.items():
        df_f[raw_col] = pd.to_numeric(df_f[raw_col], errors="coerce")
        elite_f[raw_col] = pd.to_numeric(elite_f[raw_col], errors="coerce")

        stats = elite_f.groupby("Weight_Class")[raw_col].agg(m="mean", s="std").reset_index()
        df_f = df_f.merge(stats, on="Weight_Class", how="left")

        df_f[z_col] = np.where(
            (df_f["s"] > 0),
            (df_f[raw_col] - df_f["m"]) / df_f["s"],
            0.0
        )

        df_f[z_col] = df_f[z_col].round(3).fillna(0.0)
        df_f.drop(columns=["m", "s"], inplace=True)

    # ==========================================
    # PART B. True Z-Score 계산
    # ==========================================
    print(" 🔄 Match_History 기반 True Z-Score 계산 중...")

    if not df_h.empty and "Fighter" in df_h.columns:
        df_h["f_id"] = df_h["Fighter"].apply(lambda x: x[0] if isinstance(x, list) else x)

        # 필요한 칼럼이 없는 경우 방어
        for col in ["True_Str_Landed", "True_TD_Landed", "True_TD_Defended", "True_Str_Defended"]:
            if col not in df_h.columns:
                df_h[col] = np.nan

        true_agg = df_h.groupby("f_id").agg({
            "True_Str_Landed": "mean",
            "True_TD_Landed": "mean",
            "True_TD_Defended": "mean",
            "True_Str_Defended": "mean"
        }).reset_index()

        df_f = df_f.merge(true_agg, left_on="record_id", right_on="f_id", how="left")

        true_z_map = {
            "True_Str_Landed": "True_z_slpm",
            "True_TD_Landed": "True_z_td_avg",
            "True_TD_Defended": "True_z_tdd",
            "True_Str_Defended": "True_z_str_def"
        }

        for raw_col, z_col in true_z_map.items():
            elite_t = df_f[
                df_f["Current_Ranking"].notna() &
                (df_f["Current_Ranking"] != "Unranked") &
                (df_f["Current_Ranking"] != "Prospect")
            ].copy()

            if raw_col in elite_t.columns:
                elite_t[raw_col] = pd.to_numeric(elite_t[raw_col], errors="coerce")

                t_stats = elite_t.groupby("Weight_Class")[raw_col].agg(m="mean", s="std").reset_index()
                df_f = df_f.merge(t_stats, on="Weight_Class", how="left", suffixes=("", "_tmp"))

                df_f[z_col] = np.where(
                    (df_f["s"] > 0),
                    (df_f[raw_col] - df_f["m"]) / df_f["s"],
                    0.0
                )

                df_f[z_col] = df_f[z_col].round(3).fillna(0.0)
                df_f.drop(columns=["m", "s"], inplace=True)
            else:
                df_f[z_col] = 0.0
    else:
        print(" ⚠️ Match_History가 비어 있거나 Fighter 칼럼이 없어 True Z-Score는 0.0으로 처리")
        for z_col in ["True_z_slpm", "True_z_td_avg", "True_z_tdd", "True_z_str_def"]:
            df_f[z_col] = 0.0

    # ==========================================
    # PART C. Airtable 일괄 업데이트
    # ==========================================
    print(" 🚀 계산된 Z-Score를 Airtable에 반영 중...")

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
            "True_z_str_def": float(row.get("True_z_str_def", 0.0))
        }
        update_batch.append({
            "id": row["record_id"],
            "fields": payload
        })

    try:
        for i in range(0, len(update_batch), 10):
            f_table.batch_update(update_batch[i:i+10])
    except Exception as e:
        print(f"❌ Airtable 일괄 업데이트 실패: {e}")
        return False

    print(f"✅ 총 {len(update_batch)}명! Z-Score 업데이트 완료!")
    return True


def run_prospect_pipeline():
    """
    유망주 전용 통합 파이프라인
    1. 타깃 유망주를 Fighters 테이블에 적재
    2. 적재 후 z-score 재계산
    """
    print("🔥 [유망주 통합 파이프라인] 시작")

    add_ok = add_target_prospects()
    if not add_ok:
        print("❌ 유망주 적재 단계에서 실패. Z-Score 계산은 생략.")
        return

    z_ok = update_z_scores_only()
    if not z_ok:
        print("❌ Z-Score 업데이트 단계 실패")
        return

    print("\n🏆 유망주 적재 + Z-Score 업데이트 전체 완료!")


if __name__ == "__main__":
    run_prospect_pipeline()