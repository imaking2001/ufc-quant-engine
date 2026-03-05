import re
from datetime import datetime
import unicodedata

def get_name_tokens(name):
    """이름 표기 오류 및 동명이인 방어를 위해 이름을 쪼개고 정제하는 함수"""
    if not name: return set()
    n = name.lower()
    n = unicodedata.normalize('NFKD', n).encode('ASCII', 'ignore').decode('utf-8')
    n = re.sub(r'[^a-z0-9\s]', '', n)
    return set(n.split())

def convert_height(h_str):
    """5' 10" 같은 피트/인치를 cm로 변환"""
    try:
        match = re.search(r"(\d+)'\s*(\d+)", h_str)
        if match:
            feet = int(match.group(1))
            inches = int(match.group(2))
            return round((feet * 12 + inches) * 2.54, 1)
    except: pass
    return None

def convert_reach(r_str):
    """인치를 cm로 변환"""
    try:
        match = re.search(r"(\d+(\.\d+)?)", r_str)
        if match:
            return round(float(match.group(1)) * 2.54, 1)
    except: pass
    return None

def calculate_age(dob_str):
    """생년월일(Jul 14, 1988)을 바탕으로 현재 나이 계산"""
    try:
        dob = datetime.strptime(dob_str.strip(), '%b %d, %Y')
        today = datetime.today()
        return today.year - dob.year - ((today.month, today.day) < (dob.month, dob.day))
    except: pass
    return None

def clean_float(txt):
    """문자열에서 소수점 숫자만 추출"""
    try:
        match = re.search(r"[-+]?\d*\.\d+|\d+", txt)
        if match: return float(match.group())
    except: pass
    return 0.0

def clean_percent(txt):
    """% 문자열을 소수로 변환 (예: 50% -> 0.5)"""
    try:
        match = re.search(r"(\d+(\.\d+)?)", txt)
        if match: return float(match.group(1)) / 100.0
    except: pass
    return 0.0

def guess_weight_class(weight_str):
    """몸무게(lbs)를 바탕으로 체급 추정"""
    try:
        match = re.search(r"(\d+)", weight_str)
        if match:
            lbs = int(match.group(1))
            if lbs <= 125: return "Flyweight"
            elif lbs <= 135: return "Bantamweight"
            elif lbs <= 145: return "Featherweight"
            elif lbs <= 155: return "Lightweight"
            elif lbs <= 170: return "Welterweight"
            elif lbs <= 185: return "Middleweight"
            elif lbs <= 205: return "Light Heavyweight"
            else: return "Heavyweight"
    except: pass
    return "Unknown"