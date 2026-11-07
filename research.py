
import json, os, re, time, urllib.parse, urllib.request, urllib.error
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from openai import OpenAI

OUTPUT = Path("intel.json")
HISTORY = Path("intel_history.json")
MODEL = os.getenv("OPENAI_MODEL", "gpt-4.1-mini")

CATEGORIES = [
    "michigan", "federal", "virginia",
    "florida", "tennessee"
]

SEARCHES = {
    "michigan": [
        "Michigan cannabis SB 597 licensing tax",
        "Michigan marijuana 24% wholesale tax court",
        "Michigan CRA cannabis recall enforcement",
        "Michigan cannabis wholesale prices supply",
        "dispensary closing opening Alpena Gaylord Cheboygan Manistee Lowell Cedar Springs Rogers City"
    ],
    "federal": [
        "DEA marijuana rescheduling Schedule III 280E",
        "Congress federal hemp THC law regulation",
        "DOJ DEA cannabis enforcement tax policy"
    ],
    "virginia": [
        "Virginia adult use cannabis retail licensing Cannabis Control Authority",
        "Virginia marijuana retail legalization legislation"
    ],
    "florida": [
        "Florida hemp THC law licensing enforcement"
    ],
    "tennessee": [
        "Tennessee hemp THCA flower law licensing enforcement"
    ]
}

FEEDS = {
    "michigan": [
        "https://www.michigan.gov/cra/rss"
    ],
    "federal": [
        "https://www.federalregister.gov/documents/search.rss?conditions%5Bterm%5D=marijuana"
    ],
    "virginia": [],
    "florida": [],
    "tennessee": []
}

HEADERS = {
    "User-Agent": "MedsCafeIntel/1.0",
    "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml, */*"
}

def load_json(path, default):
    try:
        value = json.loads(path.read_text()) if path.exists() else default
        return value if isinstance(value, dict) else default
    except (OSError, ValueError):
        return default

def request_bytes(url, attempts=2):
    last = None
    for attempt in range(attempts):
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=16) as response:
                return response.read(1200000)
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError) as exc:
            last = exc
            if attempt + 1 < attempts:
                time.sleep(1.5 * (attempt + 1))
    raise last

def normalize(text):
    return re.sub(r"\W+", " ", str(text).casefold()).strip()

def parse_feed(data, source_url):
    root = ET.fromstring(data)
    items = []

    for item in root.findall(".//item")[:20]:
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        date = (item.findtext("pubDate") or "").strip()
        source = item.find("source")
        publisher = (
            (source.text or "").strip()
            if source is not None
            else urllib.parse.urlparse(source_url).netloc
        )

        if title and link:
            items.append({
                "title": title,
                "url": link,
                "date": date,
                "publisher": publisher,
                "description": re.sub(
                    "<[^>]+>", " ",
                    item.findtext("description") or ""
                )[:450]
            })

    ns = "{http://www.w3.org/2005/Atom}"
    for item in root.findall(".//" + ns + "entry")[:20]:
        title = (item.findtext(ns + "title") or "").strip()
        link_el = item.find(ns + "link")
        link = link_el.get("href", "") if link_el is not None else ""
        date = item.findtext(ns + "updated") or ""

        if title and link:
            items.append({
                "title": title,
                "url": link,
                "date": date,
                "publisher": urllib.parse.urlparse(source_url).netloc,
                "description": ""
            })

    return items

def empty_update():
    return {
        "headline": "No verified change",
        "summary": "",
        "impact": "",
        "url": "",
        "significant": False,
        "date": "",
        "recommended_action": "",
        "verification": "unverified"
    }

def fetch_all():
    articles = {c: [] for c in CATEGORIES}
    errors = []
    successful_sources = 0

    for category in CATEGORIES:
        for feed in FEEDS[category]:
            try:
                items = parse_feed(request_bytes(feed), feed)
                articles[category].extend(items)
                successful_sources += 1
            except Exception as exc:
                errors.append(
                    f"{category} feed {feed}: {type(exc).__name__}: {exc}"
                )

        for query in SEARCHES[category]:
            params = urllib.parse.urlencode({
                "q": query + " when:7d",
                "hl": "en-US",
                "gl": "US",
                "ceid": "US:en"
            })
            url = "https://news.google.com/rss/search?" + params

            try:
                items = parse_feed(request_bytes(url), url)
                articles[category].extend(items)
                successful_sources += 1
            except Exception as exc:
                errors.append(
                    f"{category} Google RSS: {type(exc).__name__}: {exc}"
                )

        dedup = {}
        for item in articles[category]:
            dedup.setdefault(normalize(item["title"]), item)
        articles[category] = list(dedup.values())[:45]

    return articles, errors, successful_sources

def analyze(client, articles, now):
    instructions = """
You analyze cannabis industry news leads for Meds Cafe.

Return a JSON object with arrays:
michigan, federal, virginia, florida, tennessee.

Include up to five items per array.

Each item must contain:
headline, summary, impact, url, date, recommended_action.

Use only supplied URLs and dates.
Do not invent or infer regulatory changes from headlines.

These are UNVERIFIED leads, not verified alerts.

Prioritize:
Michigan cannabis stores, SB 597, 24% tax,
CRA enforcement, wholesale prices,
federal 280E and rescheduling,
hemp policy, Virginia licensing,
competitor closures and acquisitions.

Omit irrelevant leads.
Do not label anything verified or significant.
"""

    response = client.chat.completions.create(
        model=MODEL,
        response_format={"type": "json_object"},
        temperature=0.1,
        messages=[
            {"role": "system", "content": instructions},
            {
                "role": "user",
                "content": json.dumps({
                    "today_utc": now,
                    "articles": articles
                })
            }
        ]
    )

    result = json.loads(response.choices[0].message.content)
    return result if isinstance(result, dict) else {}

def main():
    now = datetime.now(timezone.utc).isoformat()
    history = load_json(HISTORY, {
        "seen": [],
        "alerts": [],
        "latest": {}
    })

    articles, errors, successful_sources = fetch_all()
    total = sum(len(v) for v in articles.values())
    parsed = {}

    if total:
        try:
            client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])
            parsed = analyze(client, articles, now)
        except Exception as exc:
            errors.append(
                f"AI analysis: {type(exc).__name__}: {exc}"
            )

    output = {
        "updated_at": now,
        "status": (
            "unavailable" if total == 0
            else "partial" if errors
            else "ok"
        )
    }

    for category in CATEGORIES:
        allowed = {a["url"] for a in articles[category]}
        raw = parsed.get(category, [])

        if not isinstance(raw, list):
            raw = []

        cleaned = []
        for item in raw[:5]:
            if not isinstance(item, dict):
                continue
            if item.get("url") not in allowed:
                continue

            cleaned.append({
                "headline": str(item.get("headline", ""))[:220],
                "summary": str(item.get("summary", ""))[:500],
                "impact": str(item.get("impact", ""))[:400],
                "url": item["url"],
                "date": str(item.get("date", ""))[:60],
                "significant": False,
                "recommended_action": str(
                    item.get("recommended_action", "")
                )[:400],
                "verification": "unverified"
            })

        output[category + "_updates"] = cleaned
        output[category] = (
            cleaned[0] if cleaned else empty_update()
        )

        if not cleaned and (
            total == 0 or not articles[category]
        ):
            output[category]["headline"] = "Research unavailable"

    output["alerts"] = []
    output["alert_count"] = 0
    output["errors"] = errors
    output["source_success_count"] = successful_sources
    output["article_count"] = total

    OUTPUT.write_text(
        json.dumps(output, indent=2, ensure_ascii=False) + "\n"
    )

    titles = [
        normalize(a["title"])
        for group in articles.values()
        for a in group
    ]

    history["seen"] = list(dict.fromkeys(
        history.get("seen", []) + titles
    ))[-1500:]

    history["latest"] = {
        c: output[c] for c in CATEGORIES
    }

    HISTORY.write_text(
        json.dumps(history, indent=2, ensure_ascii=False) + "\n"
    )

    print(
        f"Meds Cafe intelligence updated: {now} "
        f"articles: {total} alerts: 0 "
        f"errors: {len(errors)} status: {output['status']}"
    )

if __name__ == "__main__":
    main()
