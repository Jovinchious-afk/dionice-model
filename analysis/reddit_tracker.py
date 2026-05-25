"""
Scrapes Reddit for stock ticker mentions using Reddit's public JSON API.
No API key or PRAW required — uses public endpoints only.
Reddit is treated as an ANTI-SIGNAL: high hype (score >= 7) blocks BUY recommendations.
"""

import re
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any

import requests

SUBREDDITS = [
    "stocks",
    "StockMarket",
    "investing",
    "SecurityAnalysis",
    "tradewithcongress",
]

# Matches stock tickers: 1-5 uppercase letters preceded by $ or whitespace
TICKER_PATTERN = re.compile(r"(?:^|\s|\$)([A-Z]{1,5})(?:\s|$|[.,;!?])")

FALSE_POSITIVES = {
    # Common English words
    "A", "I", "AM", "AN", "AS", "AT", "BE", "BUT", "BY", "CAN", "DO",
    "FOR", "GET", "GO", "GOT", "HAD", "HAS", "HAVE", "HE", "HER", "HIM",
    "HIS", "HOW", "IF", "IN", "INTO", "IS", "IT", "ITS", "JUST", "KEEP",
    "KNOW", "LAST", "LET", "LIKE", "LOOK", "MAY", "ME", "MORE", "MUCH",
    "MY", "NEW", "NO", "NOT", "NOW", "OF", "OFF", "ON", "ONE", "ONLY",
    "OR", "OUR", "OUT", "OWN", "PAST", "PER", "SAY", "SEE", "SET", "SO",
    "SOME", "STILL", "SUCH", "TAKE", "THAT", "THE", "THEM", "THEN",
    "THEY", "THIS", "TIME", "TO", "TOO", "UP", "US", "USE", "VERY",
    "WAS", "WAY", "WE", "WELL", "WHEN", "WILL", "WITH", "YEAR", "YET",
    "YOU", "YOUR", "AGO", "ALSO", "BACK", "BEEN", "BOTH", "CAME", "COME",
    "DID", "DOWN", "EACH", "FROM", "GIVE", "GOOD", "GREW", "GROW", "HAND",
    "HERE", "HIGH", "HOLD", "HOPE", "JUST", "LESS", "LONG", "MADE", "MAKE",
    "MANY", "MOST", "MOVE", "MUCH", "NEED", "NEXT", "OPEN", "OVER", "PAID",
    "PLAN", "PLAY", "PUTS", "REAL", "SAME", "SAYS", "SEEM", "SELL", "SENT",
    "SHOW", "SIDE", "SIGN", "SOLD", "SOON", "STAY", "STOP", "THAN", "THINK",
    "TOLD", "TOOK", "TRUE", "TURN", "USED", "WAIT", "WANT", "WELL", "WENT",
    "WERE", "WHAT", "WHO", "WHY", "WORK", "YEAR",
    # Finance terms (not tickers)
    "AI", "CEO", "CFO", "COO", "CTO", "IPO", "ETF", "ETH", "BTC", "USA",
    "GDP", "FED", "SEC", "NYSE", "NASDAQ", "IMO", "TBH", "FOMO", "ATH",
    "DD", "TA", "PE", "EPS", "FCF", "YOY", "QOQ", "YTD", "EV", "EBITDA",
    "HODL", "YOLO", "WSB", "RH", "IRA", "ROTH", "SPAC", "REPO", "FOMC",
    "CPI", "PPI", "PMI", "NFP", "GDP", "ECB", "BOJ", "SNB", "RBA",
    "ATM", "OTM", "ITM", "PUT", "CALL", "VIX", "SPX", "NDX", "DJI",
    "ABOUT", "ABOVE", "AFTER", "AGAIN", "PRICE", "STOCK", "SHARE", "TRADE",
    "MONTH", "WEEK", "TODAY", "BASED", "MIGHT", "COULD", "WOULD", "SHOULD",
    "GOING", "THINK", "GREAT", "SMALL", "LARGE", "EARLY", "LATE", "RATE",
    "CASH", "DEBT", "LOSS", "GAIN", "RISE", "FALL", "DROP", "PUMP", "DUMP",
    "NEWS", "COST", "RISK", "BULL", "BEAR", "LONG", "SHORT", "CALL", "PUTS",
    "FUND", "BANK", "LOAN", "BOND", "NOTE", "BILL", "GOLD", "OIL", "GAS",
    "ALL", "AND", "ANY", "ARE", "DATA", "EVEN", "EVER", "EVERY", "CASE",
    # Frequently misidentified — seen in test runs
    "COMES", "FEW", "ALONE", "DOING", "LOT", "HEAVY", "LOOKS", "BELOW",
    "GAP", "NAMES", "ABOVE", "BEEN", "BOTH", "CAME", "COME", "DOES",
    "DONE", "EACH", "ELSE", "ENDS", "EVEN", "FEEL", "FELT", "GAVE",
    "GETS", "GONE", "GUYS", "HALF", "HOPE", "HUGE", "INTO", "ITEM",
    "JOBS", "LAID", "LESS", "LETS", "LIES", "LIKE", "MANY", "MEAN",
    "MINE", "MISS", "MODE", "MOVE", "MUST", "NEAR", "ONES", "OPEN",
    "PAID", "PICK", "PLAN", "PLAY", "PUTS", "REAL", "RISK", "ROLE",
    "RUNS", "SAID", "SAME", "SAYS", "SEEN", "SELF", "SETS", "SHED",
    "SHOT", "SHOW", "SIDE", "SIGN", "SITS", "SORT", "SPAN", "SPEC",
    "SPOT", "STAY", "STEP", "STOP", "SUIT", "SURE", "SWAP", "TAIL",
    "TALK", "TELL", "TERM", "THAN", "THEM", "THEN", "THEY", "TILL",
    "TIPS", "TOLD", "TOOK", "TOPS", "TORN", "TOSS", "TRIM", "TRIO",
    "TRIP", "TURN", "TYPE", "UNIT", "UPON", "USED", "VARY", "VERY",
    "VOID", "VOTE", "WAKE", "WALK", "WALL", "WARM", "WARN", "WARY",
    "WAYS", "WINS", "WISH", "WITH", "WORD", "WORE", "WRIT", "XBOX",
    "ZERO", "ZONE", "BOOM", "BUST", "DIPS", "FEAR", "FREE", "GOOD",
    "HATE", "HUGE", "IDEA", "LATE", "LEAD", "LOOK", "LOTS", "LOVE",
    "MEGA", "MILD", "MOON", "MOVE", "NEXT", "NICE", "ONCE", "ONES",
    "ONLY", "OWNS", "PEAK", "POOR", "PURE", "PUTS", "QUIT", "RAMP",
    "RANT", "RATS", "RIDE", "RIPE", "ROAD", "ROCK", "ROLL", "ROOF",
    "ROOM", "ROOT", "ROPE", "ROSE", "RUIN", "RUSH", "SAFE", "SAGA",
    "SAIL", "SALE", "SALT", "SANE", "SANK", "SIGH", "SLIM", "SLIP",
    "EDIT", "ELSE", "FEEL", "FIND", "FULL", "HALF", "HARD", "HELP", "IDEA",
    "ITEM", "JUST", "KEEP", "KIND", "KNOW", "LEAD", "LEFT", "LIFE", "LINE",
    "LIST", "LIVE", "LOAD", "LOOK", "MAIN", "MARK", "MEAN", "MEET", "MIND",
    "MISS", "NEAR", "NICE", "ONCE", "ONLY", "OPEN", "PART", "PASS", "PATH",
    "PICK", "PLUS", "POOR", "POST", "PUSH", "RATE", "READ", "REST", "SAVE",
    "SEND", "SORT", "STEP", "SURE", "TAKE", "TALK", "TELL", "TEST", "TEXT",
    "THAN", "THEM", "THEN", "THEY", "THUS", "TILL", "TIPS", "TOLD", "THEIR",
    "TOOL", "TOWN", "TREE", "TRIP", "TURN", "TYPE", "UNIT", "VIEW", "VOTE",
    "WALK", "WARM", "WIDE", "WIRE", "WISH", "WRAP", "ZERO", "ZONE", "PAPER",
    "WHICH", "WHERE", "WHILE", "WHOSE", "WHOLE", "WRITE", "WRONG", "EVERY",
    "THERE", "THESE", "THOSE", "THREE", "TRUST", "UNDER", "UNTIL", "USING",
    "GREAT", "GROUP", "GIVEN", "GOING", "HANDS", "HENCE", "HOUSE", "HUMAN",
    "IMAGE", "INDEX", "INNER", "INPUT", "ISSUE", "JUDGE", "KNOWN", "LARGE",
    "LATER", "LEARN", "LEGAL", "LEVEL", "LIGHT", "LIMIT", "LOCAL", "LOWER",
    "LUCKY", "MAJOR", "MEANS", "MEDIA", "MODEL", "MONEY", "MONTH", "MOVED",
    "NEVER", "NIGHT", "OFFER", "OFTEN", "ORDER", "OTHER", "OWNER", "PAPER",
    "PLACE", "POINT", "POWER", "PRESS", "PRICE", "PRIOR", "PROOF", "PURE",
    "QUERY", "QUEUE", "QUICK", "QUITE", "QUOTE", "RANGE", "REACH", "READY",
    "RIGHT", "ROUND", "ROUTE", "RULES", "SCALE", "SCOPE", "SENSE", "SHARE",
    "SHIFT", "SHOWN", "SINCE", "SIXTH", "SIZED", "SKILL", "SLATE", "SLEEP",
    "SLICE", "SLIDE", "SMART", "SPACE", "SPEED", "SPEND", "SPLIT", "STAFF",
    "STAGE", "START", "STATE", "STAYS", "STOCK", "STORE", "STUDY", "STYLE",
    "SUPER", "SURGE", "SWEET", "SWIFT", "SWING", "TABLE", "TEACH", "TERMS",
    "THEIR", "THEME", "THICK", "THING", "THINK", "THIRD", "TIGHT", "TIMES",
    "TITLE", "TODAY", "TOPIC", "TOTAL", "TOUCH", "TOUGH", "TRACE", "TRACK",
    "TRADE", "TRAIL", "TRAIN", "TREAT", "TRIAL", "TRIES", "TRULY", "TRUTH",
    "TWICE", "TYPED", "TYPES", "ULTRA", "UPPER", "USERS", "VALUE", "VIDEO",
    "VISIT", "VITAL", "VOICE", "WASTE", "WATCH", "WATER", "WEEKS", "WORDS",
    "WORLD", "WORTH", "WOULD", "WRITE", "WRONG", "YEARS", "YIELD",
}

POSTS_PER_SUBREDDIT = 25
MAX_POST_AGE_HOURS = 48
MIN_UPVOTES = 50

HEADERS = {
    "User-Agent": "dionice-model/1.0 (personal stock analysis tool)"
}


def _fetch_subreddit(subreddit: str, sort: str = "hot", limit: int = 25) -> list[dict]:
    url = f"https://www.reddit.com/r/{subreddit}/{sort}.json?limit={limit}"
    try:
        resp = requests.get(url, headers=HEADERS, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        return data.get("data", {}).get("children", [])
    except Exception as exc:
        print(f"[reddit_tracker] Failed to fetch r/{subreddit}: {exc}")
        return []


def _extract_tickers(text: str) -> list[str]:
    if not text:
        return []
    matches = TICKER_PATTERN.findall(text.upper())
    return [m for m in matches if m not in FALSE_POSITIVES and len(m) >= 2]


def _has_fundamental_analysis(text: str) -> bool:
    fa_keywords = [
        "p/e", "earnings", "revenue", "margin", "ebitda", "balance sheet",
        "cash flow", "fcf", "debt", "valuation", "forward pe", "peg ratio",
        "dividend", "buyback", "guidance", "eps", "return on equity",
        "annual report", "10-k", "10k", "q1", "q2", "q3", "q4",
    ]
    text_lower = text.lower()
    return sum(1 for kw in fa_keywords if kw in text_lower) >= 3


def scrape_reddit(hours_back: int = 48, target_tickers: list[str] | None = None) -> dict[str, dict]:
    """
    Scrapes configured subreddits and returns hype/sentiment data per ticker.
    Uses Reddit's public JSON API — no API key needed.

    Returns dict: {ticker: {mention_count, avg_upvotes, hype_score, posts[]}}
    Hype score 0-10: >=7 blocks BUY in ai_analyst.py
    """
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours_back)

    ticker_data: dict[str, dict] = defaultdict(lambda: {
        "mention_count": 0,
        "total_upvotes": 0,
        "posts_with_fa": 0,
        "posts": [],
    })

    for sub_name in SUBREDDITS:
        posts = _fetch_subreddit(sub_name, sort="hot", limit=POSTS_PER_SUBREDDIT)
        time.sleep(1.5)  # polite delay between subreddit requests

        for item in posts:
            post = item.get("data", {})
            created_utc = post.get("created_utc", 0)
            created = datetime.fromtimestamp(created_utc, tz=timezone.utc)

            if created < cutoff:
                continue
            score = post.get("score", 0)
            if score < MIN_UPVOTES:
                continue

            title = post.get("title", "")
            body = post.get("selftext", "") or ""
            full_text = f"{title} {body}"
            tickers = _extract_tickers(full_text)
            if not tickers:
                continue

            has_fa = _has_fundamental_analysis(full_text)

            for ticker in tickers:
                if target_tickers and ticker not in target_tickers:
                    continue
                ticker_data[ticker]["mention_count"] += 1
                ticker_data[ticker]["total_upvotes"] += score
                if has_fa:
                    ticker_data[ticker]["posts_with_fa"] += 1
                ticker_data[ticker]["posts"].append({
                    "subreddit": sub_name,
                    "title": title[:120],
                    "upvotes": score,
                    "url": f"https://reddit.com{post.get('permalink', '')}",
                    "has_fa": has_fa,
                    "created": created.strftime("%Y-%m-%d %H:%M"),
                })

    result = {}
    for ticker, data in ticker_data.items():
        count = data["mention_count"]
        avg_upvotes = data["total_upvotes"] / count if count > 0 else 0
        fa_ratio = data["posts_with_fa"] / count if count > 0 else 0

        base = min(5, count * 0.5)
        upvote_boost = min(3, avg_upvotes / 1000)
        fa_reduction = fa_ratio * 2
        hype_score = round(min(10, max(0, base + upvote_boost - fa_reduction)), 1)

        if hype_score >= 9:
            verdict = "AVOID — extreme hype"
        elif hype_score >= 7:
            verdict = "WATCHLIST only — hype blocks BUY"
        elif hype_score >= 4:
            verdict = "Moderate attention — monitor"
        else:
            verdict = "Low noise — OK for analysis"

        result[ticker] = {
            "ticker": ticker,
            "mention_count": count,
            "avg_upvotes": round(avg_upvotes),
            "posts_with_fa": data["posts_with_fa"],
            "hype_score": hype_score,
            "verdict": verdict,
            "posts": data["posts"][:5],
        }

    return result


def get_tickers_from_reddit(hours_back: int = 48, min_mentions: int = 2) -> list[str]:
    """Returns list of tickers mentioned at least min_mentions times."""
    try:
        data = scrape_reddit(hours_back)
        return [
            ticker for ticker, d in data.items()
            if d["mention_count"] >= min_mentions
        ]
    except Exception as exc:
        print(f"[reddit_tracker] get_tickers_from_reddit failed: {exc}")
        return []
