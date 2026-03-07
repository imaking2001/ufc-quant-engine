import requests

from config import ODDS_API_KEY

SPORT = "mma_mixed_martial_arts" # 격투기/UFC 코드
REGIONS = "us" # 미국 마켓 (DraftKings, BetMGM 등 기준)
MARKETS = "h2h" # 승무패 배당률 (Head to Head)
ODDS_FORMAT = "decimal" # 소수점 배당률 (예: 1.50)

def test_odds_api():
    print("🌐 무인 정찰기(The Odds API) 이륙 준비 중...")
    url = f"https://api.the-odds-api.com/v4/sports/{SPORT}/odds/"
    
    params = {
        "apiKey": ODDS_API_KEY,
        "regions": REGIONS,
        "markets": MARKETS,
        "oddsFormat": ODDS_FORMAT,
    }
    
    response = requests.get(url, params=params)
    
    if response.status_code != 200:
        print(f"❌ 에러 발생 (코드 {response.status_code}): 키가 틀렸거나 한도를 초과했어.")
        print(response.text)
        return
        
    data = response.json()
    print(f"✅ 통신 성공! 이번 주 예정된 매치업 {len(data)}개를 긁어왔어.\n")
    print("=" * 50)
    
    for idx, event in enumerate(data[:10]): # 너무 많을 수 있으니 상위 10개만 출력
        # 홈/어웨이 팀(선수) 이름
        fighter_a = event.get('home_team')
        fighter_b = event.get('away_team')
        
        print(f"🥊 매치업 {idx+1}: {fighter_a} vs {fighter_b}")
        
        # 첫 번째 북메이커(베팅 사이트)의 배당률 가져오기
        if event.get('bookmakers'):
            bookmaker = event['bookmakers'][0] 
            markets = bookmaker.get('markets', [])
            if markets:
                outcomes = markets[0].get('outcomes', [])
                print(f"💰 글로벌 배당률 ({bookmaker['title']} 마켓 기준):")
                for outcome in outcomes:
                    print(f"  - {outcome['name']}: {outcome['price']}")
        else:
            print("💰 배당률: 아직 마켓에 배당이 안 떴음")
        print("-" * 50)

if __name__ == "__main__":
    test_odds_api()