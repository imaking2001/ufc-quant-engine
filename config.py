import os
from dotenv import load_dotenv

load_dotenv()

ODDS_API_KEY = (os.getenv("ODDS_API_KEY") or "").strip()
AIRTABLE_TOKEN = (os.getenv("AIRTABLE_TOKEN") or "").strip()
AIRTABLE_BASE_ID = (os.getenv("AIRTABLE_BASE_ID") or "").strip()
DIFY_API_KEY = (os.getenv("DIFY_API_KEY") or "").strip()

missing = []

if not ODDS_API_KEY:
    missing.append("ODDS_API_KEY")
if not AIRTABLE_TOKEN:
    missing.append("AIRTABLE_TOKEN")
if not AIRTABLE_BASE_ID:
    missing.append("AIRTABLE_BASE_ID")
if not DIFY_API_KEY:
    missing.append("DIFY_API_KEY")

if missing:
    raise ValueError(f"환경변수가 비어 있음: {', '.join(missing)}")