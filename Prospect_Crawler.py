import time
import requests
import urllib.parse
from bs4 import BeautifulSoup
from pyairtable import Api

# 🛠️ 네가 만든 완벽한 클렌징 툴킷 그대로 수입!
from ufc_utils import *

# 🔐 보안용 설정 파일에서 Airtable 정보 불러오기
# config.py는 .env 파일을 읽어서 실제 키를 가져오는 역할
from config import AIRTABLE_TOKEN, AIRTABLE_BASE_ID

# ==========================================
# 🔑 1. 에어테이블 세팅
# ==========================================
FIGHTERS_TABLE = "Fighters"

api = Api(AIRTABLE_TOKEN)
f_table = api.table(AIRTABLE_BASE_ID, FIGHTERS_TABLE)

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
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'
        }

        # 2. UFC 검색창 특성상 성(last name) 위주로 넓게 검색
        name_parts = name.split()
        search_keyword = name_parts[-1]

        # 예: Raul Rosas Jr -> Jr는 실제 성이 아니므로 그 앞 단어 사용
        if search_keyword.lower() in ['jr', 'jr.', 'sr', 'sr.'] and len(name_parts) > 1:
            search_keyword = name_parts[-2]

        print(f"      ➔ [디버그] '{search_keyword}' 키워드로 UFC 서버 우회 스캔 중...")

        res = requests.get(
            f"http://ufcstats.com/statistics/fighters/search?query={urllib.parse.quote(search_keyword)}",
            headers=headers,
            timeout=15
        )
        res.raise_for_status()

        soup = BeautifulSoup(res.text, 'html.parser')
        rows = soup.select("tr.b-statistics__table-row")
        target_tokens = get_name_tokens(name)

        for row in rows:
            cols = row.select("td")

            # 데이터가 실제로 있는 행만 처리
            if len(cols) > 1 and cols[0].find("a"):
                found = cols[0].get_text(strip=True) + " " + cols[1].get_text(strip=True)
                found_tokens = get_name_tokens(found)

                # 이름 토큰이 완전히 같거나, 교집합이 충분히 크면 같은 인물로 판단
                if target_tokens == found_tokens or len(target_tokens.intersection(found_tokens)) >= 2:
                    return cols[0].find("a")['href']

    except requests.exceptions.Timeout:
        print("      ➔ URL 스캔 중 에러: UFC 서버 응답 시간이 너무 길어 timeout 발생")
    except requests.exceptions.RequestException as e:
        print(f"      ➔ URL 스캔 중 네트워크/HTTP 에러: {e}")
    except Exception as e:
        print(f"      ➔ URL 스캔 중 예상치 못한 에러: {e}")

    return None


def run_sniper():
    print("🔄 에어테이블 기존 명단 확인 중...")

    try:
        existing_records = f_table.all()
    except Exception as e:
        print(f"❌ 에어테이블 기존 데이터 조회 실패: {e}")
        print("   ➔ Airtable 토큰, Base ID, 테이블명(Fighters) 확인 필요")
        return

    existing_tokens_list = [
        get_name_tokens(r['fields'].get('Name', ''))
        for r in existing_records
    ]

    print(f"🔫 [스나이퍼 가동] 총 {len(TARGET_FIGHTERS)}명의 타겟 추적 시작...")

    for name in TARGET_FIGHTERS:
        print(f"\n🔍 타겟: {name}")

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
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'
            }

            # 타겟 크롤링 및 파싱 파트
            res = requests.get(url, headers=headers, timeout=15)
            res.raise_for_status()
            soup = BeautifulSoup(res.text, 'html.parser')

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

            # 체급 정보가 비어 있으면 weight 기반으로 추정
            if not upd.get('Weight_Class'):
                gc = guess_weight_class(weight_lbs)
                if gc != "Unknown":
                    upd["Weight_Class"] = gc

            # 실제 경기 행이 있는 것만 카운트
            upd["UFC_Fights"] = sum(
                1 for row in soup.select("tr.b-fight-details__table-row") if row.find("td")
            )

            # Airtable 적재
            f_table.create(upd)
            print(f"   ➔ ✅ [타겟 적재 완료] {name} (체급: {upd.get('Weight_Class', '확인불가')})")

            # 이미 적재한 선수는 중복 방지용 목록에 추가
            existing_tokens_list.append(target_tokens)

        except requests.exceptions.Timeout:
            print("   ➔ ❌ 크롤링 에러: UFC Stats 페이지 응답 timeout")
        except requests.exceptions.RequestException as e:
            print(f"   ➔ ❌ 크롤링 네트워크/HTTP 에러: {e}")
        except Exception as e:
            print(f"   ➔ ❌ 크롤링/업로드 에러: {e}")

        # UFC 서버 과부하 방지용 텀
        time.sleep(1)

    print("\n🏁 모든 타겟 처리 완료. 에어테이블 확인해 봐!")


if __name__ == "__main__":
    run_sniper()