import time
import requests
import urllib.parse
from bs4 import BeautifulSoup
from pyairtable import Api

# 🛠️ 네가 만든 완벽한 클렌징 툴킷 그대로 수입!
from ufc_utils import *

# ==========================================
# 🔑 1. 에어테이블 세팅
# ==========================================
AIRTABLE_API_KEY = "pat2Gb9ZFl6W9eVhU.beaf37ed6a9eea30d5867442356757c6f12ba50c641fcb1c50e10c8a3853a929"
BASE_ID = "appMzf13OXWbFtYnG"
FIGHTERS_TABLE = "Fighters"

api = Api(AIRTABLE_API_KEY)
f_table = api.table(BASE_ID, FIGHTERS_TABLE)

# ==========================================
# 🎯 2. 암살 타겟 명단
# ==========================================
TARGET_FIGHTERS = [
    "Michael Johnson",
    "Jesus Santos Aguilar",
    "Nyamjargal Tumendemberel",
    "Raul Rosas Jr"
]

def search_fighter_url(name):
    """UFC 검색창의 한계를 뚫어버리는 샷건+스나이퍼 우회 로직"""
    try:
        # 1. 봇 차단 방어 (크롬 브라우저인 척 위장)
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}
        
        # 2. 멍청한 UFC 검색창을 위해 '성(Last Name)'만 추출해서 넓게 검색
        name_parts = name.split()
        search_keyword = name_parts[-1]
        # 만약 끝 단어가 Jr 이면 그 앞 단어(진짜 성)를 키워드로 사용
        if search_keyword.lower() in ['jr', 'jr.', 'sr', 'sr.'] and len(name_parts) > 1:
            search_keyword = name_parts[-2]

        print(f"      ➔ [디버그] '{search_keyword}' 키워드로 UFC 서버 우회 스캔 중...")
        res = requests.get(f"http://ufcstats.com/statistics/fighters/search?query={urllib.parse.quote(search_keyword)}", headers=headers)
        soup = BeautifulSoup(res.text, 'html.parser')
        
        # 3. [2:] 슬라이싱의 함정 제거! 모든 행을 가져와서 'td'가 있는지만 판별해 첫 결과 누락 방어.
        rows = soup.select("tr.b-statistics__table-row")
        target_tokens = get_name_tokens(name)
        
        for row in rows:
            cols = row.select("td")
            if len(cols) > 1 and cols[0].find("a"): # 데이터가 존재하는 행만 필터링
                found = cols[0].get_text(strip=True) + " " + cols[1].get_text(strip=True)
                found_tokens = get_name_tokens(found)
                
                # 4. 네가 만든 교집합(intersection) 로직으로 진짜 타겟 색출
                if target_tokens == found_tokens or len(target_tokens.intersection(found_tokens)) >= 2:
                    return cols[0].find("a")['href']
    except Exception as e:
        print(f"      ➔ URL 스캔 중 에러: {e}")
    return None

def run_sniper():
    print("🔄 에어테이블 기존 명단 확인 중...")
    existing_records = f_table.all()
    existing_tokens_list = [get_name_tokens(r['fields'].get('Name', '')) for r in existing_records]
    
    print(f"🔫 [스나이퍼 가동] 총 {len(TARGET_FIGHTERS)}명의 타겟 추적 시작...")
    
    for name in TARGET_FIGHTERS:
        print(f"\n🔍 타겟: {name}")
        
        target_tokens = get_name_tokens(name)
        if target_tokens in existing_tokens_list:
            print(f"   ➔ ⚠️ 이미 에어테이블에 존재. 패스!")
            continue
            
        url = search_fighter_url(name)
        if not url:
            print(f"   ➔ ❌ URL 찾기 실패. UFC 서버에 해당 선수가 없는 것 같아.")
            continue
            
        print(f"   ➔ 🔗 URL 확보 완료: {url}")
        
        try:
            # 타겟 크롤링 및 파싱 파트
            res = requests.get(url)
            soup = BeautifulSoup(res.text, 'html.parser')
            
            upd = {
                "Name": name.title(),
                "Current_Ranking": "Prospect",
                "UFC_Stats_URL": url
            }
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
            
            if not upd.get('Weight_Class'):
                gc = guess_weight_class(weight_lbs)
                if gc != "Unknown": upd["Weight_Class"] = gc
                
            upd["UFC_Fights"] = sum(1 for row in soup.select("tr.b-fight-details__table-row") if row.find("td"))
            
            f_table.create(upd)
            print(f"   ➔ ✅ [타겟 적재 완료] {name} (체급: {upd.get('Weight_Class', '확인불가')})")
            existing_tokens_list.append(target_tokens)
            
        except Exception as e:
            print(f"   ➔ ❌ 크롤링/업로드 에러: {e}")
            
        time.sleep(1)

    print("\n🏁 모든 타겟 처리 완료. 에어테이블 확인해 봐!")

if __name__ == "__main__":
    run_sniper()