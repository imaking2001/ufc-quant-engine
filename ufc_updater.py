import time
import random
import urllib.parse
import requests
import pandas as pd
import numpy as np
from bs4 import BeautifulSoup
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from webdriver_manager.chrome import ChromeDriverManager
from pyairtable import Api

# 공통 헬퍼 함수 모듈 불러오기
from ufc_utils import *

# ==========================================
# 🚀 [엔진 모듈] ufc_updater.py (최종 통합본)
# ==========================================

def get_chrome_driver():
    """스텔스 세팅 크롬 드라이버"""
    options = webdriver.ChromeOptions()
    options.add_argument("user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64)")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    options.add_experimental_option('useAutomationExtension', False)
    return webdriver.Chrome(service=Service(ChromeDriverManager().install()), options=options)

def update_official_rankings(f_table):
    """1. 공식 랭킹 스크래핑 및 신규 랭커 추가"""
    print("\n🌐 [Phase 1] 공식 랭킹 동기화 중...")
    driver = get_chrome_driver()
    driver.get("https://kr.ufc.com/rankings")
    time.sleep(3.0)
    
    soup = BeautifulSoup(driver.page_source, 'html.parser')
    ranked_data = {}
    
    weight_class_map = {
        "플라이급": "Flyweight", "밴텀급": "Bantamweight", "페더급": "Featherweight",
        "라이트급": "Lightweight", "웰터급": "Welterweight", "미들급": "Middleweight",
        "라이트 헤비급": "Light Heavyweight", "헤비급": "Heavyweight"
    }
    
    weight_classes = soup.select(".view-grouping")
    for wc_block in weight_classes:
        wc_header = wc_block.select_one(".view-grouping-header")
        if not wc_header: continue
        
        raw_wc_name = wc_header.get_text(strip=True).replace("Top Rank", "").strip()
        wc_lower = raw_wc_name.lower()
        if "women" in wc_lower or "여성" in wc_lower or "pound" in wc_lower: continue
            
        clean_wc_name = raw_wc_name
        for kr_name in sorted(weight_class_map.keys(), key=len, reverse=True):
            if kr_name in wc_lower:
                clean_wc_name = weight_class_map[kr_name]
                break
                
        champion_tag = wc_block.select_one("#champion-image-name a") or wc_block.select_one("h5 a")
        if champion_tag:
            ranked_data[champion_tag.get_text(strip=True)] = {'weight_class': clean_wc_name, 'rank': 'Champion'}
            
        for row in wc_block.select("tr"):
            name_tag = row.select_one(".views-field-title a")
            rank_tag = row.select_one(".views-field-weight-class-rank")
            if name_tag and rank_tag:
                ranked_data[name_tag.get_text(strip=True)] = {'weight_class': clean_wc_name, 'rank': rank_tag.get_text(strip=True)}
                
    driver.quit()

    existing_records = f_table.all()
    existing_names = [r['fields'].get('Name', '') for r in existing_records]
    
    new_batch = []
    for ufc_name, data in ranked_data.items():
        ufc_tokens = get_name_tokens(ufc_name)
        is_exist = any(ufc_tokens == get_name_tokens(e_name) or (len(ufc_tokens.intersection(get_name_tokens(e_name))) >= 2) for e_name in existing_names)
        if not is_exist:
            new_batch.append({"Name": ufc_name, "Weight_Class": data['weight_class'], "Current_Ranking": str(data['rank'])})
            
    if new_batch:
        f_table.batch_create(new_batch)
        print(f"✅ 신규 랭커 {len(new_batch)}명 추가 완료.")
    else:
        print("✅ 신규 진입 랭커 없음.")

def update_gatekeepers_and_missing_stats(f_table, h_table):
    """2. 수문장 색출 및 누락된 필수 지표 타겟 업데이트"""
    print("\n🔍 [Phase 2] 수문장 탐지 및 지표 수집...")
    
    opp_counts = {}
    for record in h_table.iterate():
        for r in record:
            opp_name = r['fields'].get('Opponent', '').strip()
            if opp_name: opp_counts[opp_name] = opp_counts.get(opp_name, 0) + 1

    candidates = [name for name, count in opp_counts.items() if count >= 2]
    all_f = f_table.all()
    existing_tokens = {f['id']: get_name_tokens(f['fields'].get('Name', '')) for f in all_f}
    
    for cand in candidates:
        cand_tokens = get_name_tokens(cand)
        matched_id = next((f_id for f_id, tokens in existing_tokens.items() if cand_tokens == tokens), None)
        
        if matched_id:
            record = f_table.get(matched_id)
            if not record['fields'].get('UFC_Stats_URL'):
                url = _get_ufc_search_url(cand)
                if url: f_table.update(matched_id, {"UFC_Stats_URL": url, "Current_Ranking": record['fields'].get('Current_Ranking') or "Gatekeeper"})
        else:
            url = _get_ufc_search_url(cand)
            if url: f_table.create({"Name": cand.title(), "Current_Ranking": "Gatekeeper", "UFC_Stats_URL": url})

    # 타겟 크롤링 (빈칸 채우기)
    required = ['Str_Acc', 'Str_Def', 'TD_Acc', 'TD_Def', 'Sub_Avg', 'Weight_Class', 'Age', 'Reach', 'Height', 'UFC_Fights']
    for f in f_table.all():
        fields = f['fields']
        target_url = fields.get('UFC_Stats_URL')
        if target_url and not all(fields.get(s) is not None for s in required):
            try:
                res = requests.get(target_url)
                soup = BeautifulSoup(res.text, 'html.parser')
                upd = {}
                weight_lbs = ""
                for item in soup.select("li.b-list__box-list-item"):
                    txt = item.get_text(strip=True)
                    if "Height:" in txt: upd["Height"] = convert_height(txt.replace("Height:", ""))
                    if "Weight:" in txt: weight_lbs = txt.replace("Weight:", "").strip()
                    if "Reach:" in txt: upd["Reach"] = convert_reach(txt.replace("Reach:", ""))
                    if "DOB:" in txt: upd["Age"] = calculate_age(txt.replace("DOB:", ""))
                    if "SLpM:" in txt: upd["SLpM"] = clean_float(txt.replace("SLpM:", ""))
                    if "Str. Acc.:" in txt: upd["Str_Acc"] = clean_percent(txt.replace("Str. Acc.:", ""))
                    if "SApM:" in txt: upd["SApM"] = clean_float(txt.replace("SApM:", ""))
                    if "Str. Def" in txt: upd["Str_Def"] = clean_percent(txt.split("Def")[-1].replace(".:", "").replace(":", "").strip())
                    if "TD Avg.:" in txt: upd["TD_Avg"] = clean_float(txt.replace("TD Avg.:", ""))
                    if "TD Acc.:" in txt: upd["TD_Acc"] = clean_percent(txt.replace("TD Acc.:", ""))
                    if "TD Def.:" in txt: upd["TD_Def"] = clean_percent(txt.replace("TD Def.:", ""))
                    if "Sub. Avg.:" in txt: upd["Sub_Avg"] = clean_float(txt.replace("Sub. Avg.:", ""))
                if not fields.get('Weight_Class'):
                    gc = guess_weight_class(weight_lbs)
                    if gc != "Unknown": upd["Weight_Class"] = gc
                upd["UFC_Fights"] = sum(1 for row in soup.select("tr.b-fight-details__table-row") if row.find("td"))
                f_table.update(f['id'], upd)
                time.sleep(0.5)
            except: pass

def recalculate_z_scores(f_table, h_table):
    """3. 체급별 Proxy Z-Score 및 Match_History 기반 최종 True Z-Score 업데이트"""
    print("\n📊 [Phase 3] Proxy 및 최종 True Z-Score 이중 연산 시작...")
    
    df_f = pd.DataFrame([ {**f['fields'], 'record_id': f['id']} for f in f_table.all() ])
    df_h = pd.DataFrame([ h['fields'] for h in h_table.all() ])

    # --- PART A: 1단계 Proxy Z-Score (Official Raw 기반) ---
    elite_f = df_f[df_f['Current_Ranking'].notna() & (df_f['Current_Ranking'] != 'Unranked')].copy()
    proxy_map = {'SLpM': 'z_slpm', 'TD_Avg': 'z_td_avg', 'TD_Def': 'z_tdd', 'Str_Def': 'z_str_def'}

    for raw, z in proxy_map.items():
        df_f[raw] = pd.to_numeric(df_f[raw], errors='coerce')
        elite_f[raw] = pd.to_numeric(elite_f[raw], errors='coerce')
        stats = elite_f.groupby('Weight_Class')[raw].agg(m='mean', s='std').reset_index()
        df_f = df_f.merge(stats, on='Weight_Class', how='left')
        df_f[z] = np.where((df_f['s'] > 0), (df_f[raw] - df_f['m']) / df_f['s'], 0.0)
        df_f[z] = df_f[z].round(3).fillna(0.0)
        df_f.drop(columns=['m', 's'], inplace=True)

    # --- PART B: 3단계 최종 True Z-Score (Match_History 합산 기반) ---
    print(" 🔄 Match_History 보정 데이터(True Stat) 집계 중...")
    df_h['f_id'] = df_h['Fighter'].apply(lambda x: x[0] if isinstance(x, list) else x)
    
    true_agg = df_h.groupby('f_id').agg({
        'True_Str_Landed': 'mean', 'True_TD_Landed': 'mean',
        'True_TD_Defended': 'mean', 'True_Str_Defended': 'mean'
    }).reset_index()

    df_f = df_f.merge(true_agg, left_on='record_id', right_on='f_id', how='left')
    true_z_map = {
        'True_Str_Landed': 'True_z_slpm', 'True_TD_Landed': 'True_z_td_avg',
        'True_TD_Defended': 'True_z_tdd', 'True_Str_Defended': 'True_z_str_def'
    }

    for t_raw, t_z in true_z_map.items():
        elite_t = df_f[df_f['Current_Ranking'].notna() & (df_f['Current_Ranking'] != 'Unranked')].copy()
        t_stats = elite_t.groupby('Weight_Class')[t_raw].agg(m='mean', s='std').reset_index()
        df_f = df_f.merge(t_stats, on='Weight_Class', how='left', suffixes=('', '_tmp'))
        df_f[t_z] = np.where((df_f['s'] > 0), (df_f[t_raw] - df_f['m']) / df_f['s'], 0.0)
        df_f[t_z] = df_f[t_z].round(3).fillna(0.0)
        df_f.drop(columns=['m', 's'], inplace=True)

    # --- 에어테이블 일괄 업데이트 ---
    update_batch = []
    for _, row in df_f.iterrows():
        payload = {
            'z_slpm': float(row['z_slpm']), 'z_td_avg': float(row['z_td_avg']),
            'z_tdd': float(row['z_tdd']), 'z_str_def': float(row['z_str_def']),
            'True_z_slpm': float(row.get('True_z_slpm', 0.0)),
            'True_z_td_avg': float(row.get('True_z_td_avg', 0.0)),
            'True_z_tdd': float(row.get('True_z_tdd', 0.0)),
            'True_z_str_def': float(row.get('True_z_str_def', 0.0))
        }
        update_batch.append({"id": row['record_id'], "fields": payload})

    for i in range(0, len(update_batch), 10):
        f_table.batch_update(update_batch[i:i+10])
    print(f"✅ 총 {len(update_batch)}명 Proxy & 최종 True Z-Score 업데이트 완료!")

def _get_ufc_search_url(name):
    try:
        res = requests.get(f"http://ufcstats.com/statistics/fighters/search?query={urllib.parse.quote(name)}")
        soup = BeautifulSoup(res.text, 'html.parser')
        rows = soup.select("tr.b-statistics__table-row")[2:]
        for row in rows:
            cols = row.select("td")
            if len(cols) > 1 and cols[0].find("a"):
                found = cols[0].get_text(strip=True) + " " + cols[1].get_text(strip=True)
                if get_name_tokens(name) == get_name_tokens(found): return cols[0].find("a")['href']
    except: pass
    return None