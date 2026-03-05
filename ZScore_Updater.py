import pandas as pd
import numpy as np
from pyairtable import Api

# ==========================================
# 🔑 에어테이블 세팅
# ==========================================
AIRTABLE_API_KEY = "pat2Gb9ZFl6W9eVhU.beaf37ed6a9eea30d5867442356757c6f12ba50c641fcb1c50e10c8a3853a929"
BASE_ID = "appMzf13OXWbFtYnG"
FIGHTERS_TABLE = "Fighters"
HISTORY_TABLE = "Match_History"

api = Api(AIRTABLE_API_KEY)
f_table = api.table(BASE_ID, FIGHTERS_TABLE)
h_table = api.table(BASE_ID, HISTORY_TABLE)

def update_z_scores_only():
    print("\n📊 [Phase 3] Proxy 및 최종 True Z-Score 이중 연산 시작...")
    
    # 1. 에어테이블 데이터 불러오기
    df_f = pd.DataFrame([ {**f['fields'], 'record_id': f['id']} for f in f_table.all() ])
    df_h = pd.DataFrame([ h['fields'] for h in h_table.all() ])

    # --- PART A: 1단계 Proxy Z-Score (Official Raw 기반) ---
    # 💡 [핵심 방어] 거품 낀 'Unranked'와 'Prospect'는 랭커 평균(Elite) 계산에서 무조건 제외!
    elite_f = df_f[df_f['Current_Ranking'].notna() & 
                   (df_f['Current_Ranking'] != 'Unranked') & 
                   (df_f['Current_Ranking'] != 'Prospect')].copy()
                   
    proxy_map = {'SLpM': 'z_slpm', 'TD_Avg': 'z_td_avg', 'TD_Def': 'z_tdd', 'Str_Def': 'z_str_def'}

    for raw, z in proxy_map.items():
        df_f[raw] = pd.to_numeric(df_f[raw], errors='coerce')
        elite_f[raw] = pd.to_numeric(elite_f[raw], errors='coerce')
        
        # 체급별 랭커들의 진짜 평균(m)과 표준편차(s) 구하기
        stats = elite_f.groupby('Weight_Class')[raw].agg(m='mean', s='std').reset_index()
        df_f = df_f.merge(stats, on='Weight_Class', how='left')
        
        # 신인(Prospect)들을 포함한 모든 선수의 Z-Score 계산 후 적재
        df_f[z] = np.where((df_f['s'] > 0), (df_f[raw] - df_f['m']) / df_f['s'], 0.0)
        df_f[z] = df_f[z].round(3).fillna(0.0)
        df_f.drop(columns=['m', 's'], inplace=True)

    # --- PART B: 3단계 최종 True Z-Score (Match_History 합산 기반) ---
    print(" 🔄 Match_History 보정 데이터(True Stat) 집계 중...")
    
    # Match_History가 아예 없는 초기 상태 방어
    if not df_h.empty and 'Fighter' in df_h.columns:
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
            elite_t = df_f[df_f['Current_Ranking'].notna() & 
                           (df_f['Current_Ranking'] != 'Unranked') & 
                           (df_f['Current_Ranking'] != 'Prospect')].copy()
                           
            # 에러 방지: 칼럼이 존재하는지 확인
            if t_raw in elite_t.columns:
                t_stats = elite_t.groupby('Weight_Class')[t_raw].agg(m='mean', s='std').reset_index()
                df_f = df_f.merge(t_stats, on='Weight_Class', how='left', suffixes=('', '_tmp'))
                df_f[t_z] = np.where((df_f['s'] > 0), (df_f[t_raw] - df_f['m']) / df_f['s'], 0.0)
                df_f[t_z] = df_f[t_z].round(3).fillna(0.0)
                df_f.drop(columns=['m', 's'], inplace=True)
            else:
                df_f[t_z] = 0.0

    # --- 에어테이블 일괄 업데이트 (빈칸 채우기) ---
    print(" 🚀 에어테이블에 계산된 Z-Score 쏘는 중...")
    update_batch = []
    for _, row in df_f.iterrows():
        payload = {
            'z_slpm': float(row.get('z_slpm', 0.0)), 
            'z_td_avg': float(row.get('z_td_avg', 0.0)),
            'z_tdd': float(row.get('z_tdd', 0.0)), 
            'z_str_def': float(row.get('z_str_def', 0.0)),
            'True_z_slpm': float(row.get('True_z_slpm', 0.0)),
            'True_z_td_avg': float(row.get('True_z_td_avg', 0.0)),
            'True_z_tdd': float(row.get('True_z_tdd', 0.0)),
            'True_z_str_def': float(row.get('True_z_str_def', 0.0))
        }
        update_batch.append({"id": row['record_id'], "fields": payload})

    for i in range(0, len(update_batch), 10):
        f_table.batch_update(update_batch[i:i+10])
        
    print(f"✅ 총 {len(update_batch)}명! 랭커들의 True Z-Score & 유망주들의 Proxy Z-Score 업데이트 완료!")

if __name__ == "__main__":
    update_z_scores_only()