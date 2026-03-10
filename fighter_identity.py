import json
import os
import urllib.parse
import requests
from bs4 import BeautifulSoup

from ufc_utils import (
    get_name_tokens,
    guess_weight_class,
    convert_height,
    convert_reach,
    calculate_age,
    clean_float,
    clean_percent,
)


def is_ranked_or_gatekeeper(ranking_value: str) -> bool:
    if not ranking_value:
        return False

    r = str(ranking_value).strip().lower()

    if r == "champion":
        return True
    if r.isdigit():
        return True
    if "gatekeeper" in r:
        return True

    return False


def build_fighter_index(existing_records: list):
    indexed = []
    for r in existing_records:
        fields = r.get("fields", {})
        indexed.append(
            {
                "id": r["id"],
                "name": fields.get("Name", ""),
                "tokens": get_name_tokens(fields.get("Name", "")),
                "ranking": fields.get("Current_Ranking", ""),
                "weight_class": fields.get("Weight_Class", ""),
                "url": fields.get("UFC_Stats_URL", ""),
            }
        )
    return indexed


def find_existing_record_by_name(name: str, indexed_records: list):
    target_tokens = get_name_tokens(name)

    for rec in indexed_records:
        rec_tokens = rec["tokens"]
        if not rec_tokens:
            continue

        if target_tokens == rec_tokens:
            return rec

        if len(target_tokens.intersection(rec_tokens)) >= 2:
            return rec

    return None


def load_manual_overrides(path="manual_overrides.json"):
    if not os.path.exists(path):
        return {}

    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)

        if isinstance(data, dict):
            return data
        return {}
    except Exception:
        return {}


def save_review_queue(review_items, path="manual_review_queue.json"):
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(review_items, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"⚠️ review queue 저장 실패: {e}")


def print_exception_report(review_items, title="예외 보고서"):
    print("\n" + "=" * 70)
    print(f"📋 [{title}]")
    if not review_items:
        print("✅ 예외 사항 없음")
        print("=" * 70)
        return

    print(f"⚠️ 총 {len(review_items)}건 수동 확인 필요")
    for idx, item in enumerate(review_items, start=1):
        print("-" * 70)
        print(f"[{idx}] 이름: {item.get('target_name', '-')}")
        print(f"    이유: {item.get('reason', '-')}")
        print(f"    예상 체급: {item.get('expected_weight_class', '-')}")
        print(f"    상대/근거: {item.get('context', '-')}")
        candidates = item.get("candidates", [])
        if candidates:
            print("    후보 URL:")
            for c in candidates:
                print(f"      - {c.get('candidate_name', '-')} | {c.get('weight_class', '-')} | {c.get('url', '-')}")
    print("=" * 70)


def get_event_url(mode="upcoming"):
    if mode == "upcoming":
        page_url = "http://ufcstats.com/statistics/events/upcoming"
    else:
        page_url = "http://ufcstats.com/statistics/events/completed"

    try:
        headers = {"User-Agent": "Mozilla/5.0"}
        res = requests.get(page_url, headers=headers, timeout=20)
        res.raise_for_status()

        soup = BeautifulSoup(res.text, "html.parser")
        
        # 🚨 [테오 핫픽스 복구] 껍데기 헤더(tr)를 무시하고, 실제 대회 링크(a 태그 중 event-details 포함)를 바로 찾기
        event_link = soup.select_one("a[href*='event-details']")
        
        if not event_link:
            return None, None

        event_name = event_link.get_text(strip=True)
        event_url = event_link.get("href")
        return event_name, event_url

    except Exception as e:
        print(f"❌ 이벤트 URL 조회 실패 ({mode}): {e}")
        return None, None


def extract_fight_pairs_from_event(event_url: str):
    headers = {"User-Agent": "Mozilla/5.0"}

    try:
        res = requests.get(event_url, headers=headers, timeout=20)
        res.raise_for_status()
        soup = BeautifulSoup(res.text, "html.parser")

        fight_pairs = []
        seen_pairs = set()

        rows = soup.select("tr.b-fight-details__table-row")

        for row in rows:
            fighter_links = row.select("a[href*='fighter-details']")
            fighter_names = [a.get_text(strip=True) for a in fighter_links if a.get_text(strip=True)]

            if len(fighter_names) >= 2:
                fighter_a = fighter_names[0]
                fighter_b = fighter_names[1]

                pair_key = tuple(sorted([fighter_a.lower(), fighter_b.lower()]))
                if pair_key in seen_pairs:
                    continue

                seen_pairs.add(pair_key)
                fight_pairs.append(
                    {
                        "fighter_a": fighter_a,
                        "fighter_b": fighter_b,
                    }
                )

        return fight_pairs

    except Exception as e:
        print(f"❌ 이벤트 상세 페이지 파싱 실패: {e}")
        return []


def get_this_week_fighters():
    print("🌐 이번 주 UFC 카드 자동 스캔 중...")

    event_name, event_url = get_event_url("upcoming")

    if not event_url:
        print("⚠️ Upcoming 이벤트 조회 실패 → 최근 완료 대회 fallback")
        event_name, event_url = get_event_url("completed")

    if not event_url:
        print("❌ 스캔 가능한 UFC 이벤트를 찾지 못함")
        return None, []

    fight_pairs = extract_fight_pairs_from_event(event_url)

    print(f"✅ 대상 이벤트: {event_name}")
    print(f"✅ 감지된 매치업 수: {len(fight_pairs)}")

    return event_name, fight_pairs


def search_fighter_candidates(name):
    """
    UFC Stats 검색 결과에서 후보 목록 반환
    """
    try:
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}

        name_parts = name.split()
        search_keyword = name_parts[-1]

        if search_keyword.lower() in ["jr", "jr.", "sr", "sr."] and len(name_parts) > 1:
            search_keyword = name_parts[-2]

        res = requests.get(
            f"http://ufcstats.com/statistics/fighters/search?query={urllib.parse.quote(search_keyword)}",
            headers=headers,
            timeout=15,
        )
        res.raise_for_status()

        soup = BeautifulSoup(res.text, "html.parser")
        rows = soup.select("tr.b-statistics__table-row")

        candidates = []
        seen_urls = set()

        for row in rows:
            cols = row.select("td")
            if len(cols) > 1 and cols[0].find("a"):
                first_name = cols[0].get_text(strip=True)
                last_name = cols[1].get_text(strip=True)
                full_name = f"{first_name} {last_name}".strip()
                url = cols[0].find("a")["href"]

                if url in seen_urls:
                    continue
                seen_urls.add(url)

                candidates.append(
                    {
                        "candidate_name": full_name,
                        "url": url,
                    }
                )

        return candidates

    except Exception as e:
        print(f"❌ 후보 검색 실패: {name} / {e}")
        return []


def fetch_fighter_profile(url, fallback_name=""):
    """
    UFC Stats 상세 페이지에서 실제 프로필 파싱
    """
    headers = {"User-Agent": "Mozilla/5.0"}

    res = requests.get(url, headers=headers, timeout=15)
    res.raise_for_status()
    soup = BeautifulSoup(res.text, "html.parser")

    page_name = ""
    for selector in [
        "span.b-content__title-highlight",
        "h2.b-content__title",
        "h2",
        "title",
    ]:
        tag = soup.select_one(selector)
        if tag:
            page_name = tag.get_text(" ", strip=True)
            if page_name:
                break

    if not page_name:
        page_name = fallback_name

    upd = {
        "page_name": page_name,
        "UFC_Stats_URL": url,
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

    guessed_wc = guess_weight_class(weight_lbs)
    if guessed_wc != "Unknown":
        upd["Weight_Class"] = guessed_wc

    upd["UFC_Fights"] = sum(
        1 for row in soup.select("tr.b-fight-details__table-row") if row.find("td")
    )

    return upd


def score_identity_candidate(target_name, profile, expected_weight_class=None):
    """
    이름/체급/기본 스탯 완성도로 후보 점수 계산
    """
    score = 0
    notes = []

    target_tokens = get_name_tokens(target_name)
    page_tokens = get_name_tokens(profile.get("page_name", ""))

    if target_tokens == page_tokens:
        score += 60
        notes.append("name_exact")
    else:
        overlap = len(target_tokens.intersection(page_tokens))
        if overlap >= 2:
            score += 40
            notes.append("name_overlap_2+")
        elif overlap == 1:
            score += 15
            notes.append("name_overlap_1")

    profile_wc = profile.get("Weight_Class", "")
    if expected_weight_class:
        if profile_wc == expected_weight_class:
            score += 25
            notes.append("weight_match")
        elif profile_wc:
            score -= 40
            notes.append("weight_mismatch")

    completeness_keys = ["SLpM", "TD_Avg", "TD_Def", "Str_Def", "Weight_Class", "UFC_Fights"]
    completeness = sum(1 for k in completeness_keys if profile.get(k) not in [None, "", "Unknown"])
    score += min(completeness * 3, 15)

    age = profile.get("Age")
    reach = profile.get("Reach")
    fights = profile.get("UFC_Fights")

    if age is not None and 18 <= age <= 45:
        score += 3
    if reach is not None and 145 <= reach <= 230:
        score += 3
    if fights is not None and 0 <= fights <= 40:
        score += 4

    return score, notes


def resolve_fighter_identity(target_name, expected_weight_class=None, manual_overrides=None):
    """
    최종 신원 판별
    반환:
    {
      status: verified / ambiguous / not_found
      confidence: int
      note: str
      profile: dict or None
      candidates: [...]
    }
    """
    manual_overrides = manual_overrides or {}

    if target_name in manual_overrides:
        url = manual_overrides[target_name]
        try:
            profile = fetch_fighter_profile(url, fallback_name=target_name)
            return {
                "status": "verified",
                "confidence": 100,
                "note": "manual_override",
                "profile": profile,
                "candidates": [],
            }
        except Exception as e:
            return {
                "status": "ambiguous",
                "confidence": 0,
                "note": f"manual_override_failed: {e}",
                "profile": None,
                "candidates": [],
            }

    raw_candidates = search_fighter_candidates(target_name)
    if not raw_candidates:
        return {
            "status": "not_found",
            "confidence": 0,
            "note": "search_no_result",
            "profile": None,
            "candidates": [],
        }

    scored = []

    for c in raw_candidates[:8]:
        try:
            profile = fetch_fighter_profile(c["url"], fallback_name=c["candidate_name"])
            score, notes = score_identity_candidate(
                target_name=target_name,
                profile=profile,
                expected_weight_class=expected_weight_class,
            )
            scored.append(
                {
                    "candidate_name": c["candidate_name"],
                    "url": c["url"],
                    "weight_class": profile.get("Weight_Class", ""),
                    "score": score,
                    "notes": notes,
                    "profile": profile,
                }
            )
        except Exception:
            continue

    if not scored:
        return {
            "status": "not_found",
            "confidence": 0,
            "note": "profile_fetch_failed",
            "profile": None,
            "candidates": [],
        }

    scored.sort(key=lambda x: x["score"], reverse=True)
    top = scored[0]
    second = scored[1] if len(scored) > 1 else None

    # 체급이 맞는 후보만 따로 확인
    exact_weight_matches = []
    if expected_weight_class:
        exact_weight_matches = [c for c in scored if c.get("weight_class") == expected_weight_class]

    # 가장 깔끔한 자동 확정 조건
    if expected_weight_class and len(exact_weight_matches) == 1 and exact_weight_matches[0]["score"] >= 55:
        chosen = exact_weight_matches[0]
        return {
            "status": "verified",
            "confidence": min(chosen["score"], 100),
            "note": "single_weight_match",
            "profile": chosen["profile"],
            "candidates": scored[:5],
        }

    # 이름/점수 우위가 충분하면 자동 확정
    if top["score"] >= 75 and (second is None or (top["score"] - second["score"]) >= 15):
        return {
            "status": "verified",
            "confidence": min(top["score"], 100),
            "note": "score_clear_winner",
            "profile": top["profile"],
            "candidates": scored[:5],
        }

    # 후보가 하나뿐이고 최소 기준 넘으면 자동 확정
    if len(scored) == 1 and top["score"] >= 55:
        return {
            "status": "verified",
            "confidence": min(top["score"], 100),
            "note": "single_candidate",
            "profile": top["profile"],
            "candidates": scored[:5],
        }

    # 그 외는 애매한 후보
    return {
        "status": "ambiguous",
        "confidence": min(top["score"], 100),
        "note": "needs_manual_review",
        "profile": None,
        "candidates": scored[:5],
    }