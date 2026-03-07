from pyairtable import Api
from ufc_updater import (
    update_official_rankings, 
    update_gatekeepers_and_missing_stats, 
    recalculate_z_scores
)

# ==========================================
# ⚠️ [설정] 본인 토큰 및 베이스 정보
# ==========================================
from config import AIRTABLE_TOKEN, AIRTABLE_BASE_ID
FIGHTERS_TABLE_NAME = "Fighters"
HISTORY_TABLE_NAME = "Match_History"

def main():
    print("🔥 [마스터 파이프라인] UFC 데이터 통합 업데이트 시작...")
    
    api = Api(AIRTABLE_TOKEN)
    f_table = api.table(AIRTABLE_BASE_ID, FIGHTERS_TABLE_NAME)
    h_table = api.table(AIRTABLE_BASE_ID, HISTORY_TABLE_NAME)

    # 1. 공식 랭킹 동기화 및 신규 랭커 탐지
    update_official_rankings(f_table)
    
    # 2. 전적 기반 수문장 색출 & 빈 스탯(13개) 타겟 크롤링
    update_gatekeepers_and_missing_stats(f_table, h_table)
    
    # 3. 체급별(Weight_Class) 엘리트 기준 Proxy & True Z-Score 완벽 재계산
    recalculate_z_scores(f_table, h_table)

    print("\n🏆 모든 데이터 파이프라인 실행 완료! 완벽하게 최신화되었습니다.")

if __name__ == "__main__":
    main()