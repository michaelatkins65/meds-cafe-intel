
import json
import os
import re
import time
import urllib.parse
import urllib.request
import urllib.error
import xml.etree.ElementTree as ET

from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from openai import OpenAI

OUTPUT = Path("intel.json")
HISTORY = Path("intel_history.json")
MODEL = os.getenv("OPENAI_MODEL", "gpt-4.1-mini")

CATEGORIES = [
    "michigan",
    "federal",
    "virginia",
    "florida",
    "tennessee"
]

SEARCHES = {
    "michigan": [
        "Michigan SB 597 cannabis license cap",
        "Michigan marijuana 24 percent wholesale tax",
        "Michigan CRA cannabis recall enforcement",
        "Michigan cannabis wholesale prices supply",
        "Michigan dispensary license suspension closure",
        "Alpena Gaylord Cheboygan dispensary opening closing",
        "Lowell Cedar Springs Manistee Rogers City cannabis dispensary",
        "Michigan cannabis dispensary business for sale acquisition",
        "site:michigan.gov/cra cannabis recall",
        "site:legislature.mi.gov SB 597 marijuana"
    ],
    "federal": [
        "DEA marijuana rescheduling Schedule III 280E",
        "federal cannabis rescheduling official decision",
        "Congress hemp THC legislation",
        "IRS marijuana 280E cannabis tax",
        "federal cannabis enforcement policy"
    ],
    "virginia": [
        "Virginia adult use cannabis retail licensing",
        "Virginia Cannabis Control Authority regulations",
        "Virginia marijuana retail legalization governor"
    ],
    "florida": [
        "Florida hemp THC law licensing enforcement",
        "Florida hemp retail restrictions legislation"
    ],
    "tennessee": [
        "Tennessee THCA hemp flower law licensing",
        "Tennessee intoxicating hemp enforcement"
    ]
}

HEADERS = {
    "User-Agent": "Mozilla/5.0 MedsCafeIntel/2.0",
    "Accept": "application/json, application/rss+xml, application/xml, */*"
}


def load_json(path, default):
    try:
        if path.exists():
            value = json.loads(path.read_text())
            if isinstance(value, dict):
                return value
    except (OSError, ValueError):
        pass
    return default


def request_bytes(url, attempts=3):
    last_error = None

    for attempt in range(attempts):
        try:
            req = urllib.request.Request(
                url, headers=HEADERS
            )
            with urllib.request.urlopen(
                req, timeout=25
            ) as response:
                return response.read(2000000)

        except (
            urllib.error.HTTPError,
            urllib.error.URLError,
            TimeoutError,
            OSError
        ) as exc:
            last_error = exc
            if attempt + 1 < attempts:
                time.sleep(2 * (attempt + 1))

    raise last_error


def normalize(value):
    return re.sub(
        r"\W+", " ", str(value).casefold()
    ).strip()


def normalize_date(value):
    if not value:
        return ""

    try:
        return parsedate_to_datetime(
            value
        ).astimezone(timezone.utc).isoformat()
    except (TypeError, ValueError, OverflowError):
        return str(value)[:40]


def parse_rss(data):
    root = ET.fromstring(data)
    results = []

    for item in root.findall(".//item")[:20]:
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        date = normalize_date(
            item.findtext("pubDate") or ""
        )

        source = item.find("source")
        publisher = (
            source.text.strip()
            if source is not None and source.text
            else "Google News"
        )

        if title and link:
            results.append({
                "title": title,
                "url": link,
                "date": date,
                "publisher": publisher,
                "source_type": "news",
                "description": ""
            })

    return results


def fetch_google_news(query):
    params = urllib.parse.urlencode({
        "q": query + " when:7d",
        "hl": "en-US",
        "gl": "US",
        "ceid": "US:en"
    })

    url = (
        "https://news.google.com/rss/search?"
        + params
    )

    return parse_rss(request_bytes(url))


def fetch_federal_register(term):
    params = urllib.parse.urlencode({
        "conditions[term]": term,
        "per_page": 20,
        "order": "newest"
    })

    url = (
        "https://www.federalregister.gov/api/v1/"
        "documents.json?" + params
    )

    data = json.loads(request_bytes(url))
    results = []

    for item in data.get("results", []):
        title = item.get("title", "")
        link = item.get("html_url", "")
        date = item.get("publication_date", "")

        if not title or not link:
            continue

        results.append({
            "title": title,
            "url": link,
            "date": date,
            "publisher": "Federal Register",
            "source_type": "official_document",
            "description": (
                item.get("abstract") or ""
            )[:700],
            "document_type": item.get("type", ""),
            "document_number": item.get(
                "document_number", ""
            )
        })

    return results


def empty_update(unavailable=False):
    return {
        "headline": (
            "Research unavailable"
            if unavailable else "No verified change"
        ),
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
    success = 0
    category_success = {c: 0 for c in CATEGORIES}

    for category in CATEGORIES:
        for query in SEARCHES[category]:
            try:
                items = fetch_google_news(query)
                articles[category].extend(items)
                success += 1
                category_success[category] += 1

            except Exception as exc:
                errors.append(
                    f"{category} Google News: "
                    f"{type(exc).__name__}: {exc}"
                )

    for term in [
        "marijuana",
        "cannabis",
        "hemp tetrahydrocannabinol"
    ]:
        try:
            items = fetch_federal_register(term)
            articles["federal"].extend(items)
            success += 1
            category_success["federal"] += 1

        except Exception as exc:
            errors.append(
                f"Federal Register {term}: "
                f"{type(exc).__name__}: {exc}"
            )

    for category in CATEGORIES:
        unique = {}

        for item in articles[category]:
            key = normalize(item["title"])
            if key and key not in unique:
                unique[key] = item

        articles[category] = list(
            unique.values()
        )[:60]

    return articles, errors, success, category_success


def analyze(client, articles, now):
    instructions = """
You are a cautious business intelligence analyst
for Meds Cafe, a Michigan cannabis retailer.

Return ONLY valid JSON containing five arrays:
michigan, federal, virginia, florida, tennessee.

Each array may contain up to five relevant items.

Each item must include:
headline, summary, impact, url, date,
recommended_action.

Use only supplied articles, URLs, and dates.

Prioritize:
Michigan SB 597 and licensing restrictions.
Michigan wholesale cannabis tax.
CRA recalls and enforcement.
Competitor openings and closures.
Wholesale cannabis pricing and supply.
Acquisition opportunities.
Federal Schedule III and 280E.
Hemp regulations.
Virginia cannabis retail expansion.

Do not invent facts.

A headline is not proof that a law changed.

Federal Register documents may be official
documents, but a proposed rule is not a final rule.

Do not assert that legislation passed based
only on news headlines.

For unverified stories, clearly state uncertainty.

Recommendations should focus on practical
business consequences for Meds Cafe.

Omit irrelevant, duplicate, or stale stories.

Do not claim any item is verified or significant.

Return empty arrays when nothing useful exists.
"""

    response = client.chat.completions.create(
        model=MODEL,
        response_format={"type": "json_object"},
        temperature=0.1,
        messages=[
            {
                "role": "system",
                "content": instructions
            },
            {
                "role": "user",
                "content": json.dumps({
                    "today_utc": now,
                    "articles": articles
                })
            }
        ]
    )

    result = json.loads(
        response.choices[0].message.content
    )

    return result if isinstance(result, dict) else {}


def main():
    now = datetime.now(timezone.utc).isoformat()

    history = load_json(HISTORY, {
        "seen": [],
        "alerts": [],
        "latest": {}
    })

    articles, errors, success, category_success = (
        fetch_all()
    )

    total = sum(
        len(group) for group in articles.values()
    )

    parsed = {}
    analysis_failed = False

    if total:
        try:
            client = OpenAI(
                api_key=os.environ["OPENAI_API_KEY"]
            )

            parsed = analyze(client, articles, now)

        except Exception as exc:
            analysis_failed = True
            errors.append(
                f"AI analysis: "
                f"{type(exc).__name__}: {exc}"
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
        allowed = {
            item["url"]: item
            for item in articles[category]
        }

        raw = parsed.get(category, [])

        if not isinstance(raw, list):
            raw = []

        cleaned = []

        for item in raw[:5]:
            if not isinstance(item, dict):
                continue

            url = item.get("url", "")

            if url not in allowed:
                continue

            original = allowed[url]

            entry = {
                "headline": str(
                    item.get("headline", "")
                )[:220],
                "summary": str(
                    item.get("summary", "")
                )[:500],
                "impact": str(
                    item.get("impact", "")
                )[:400],
                "url": url,
                "date": original.get("date", ""),
                "significant": False,
                "recommended_action": str(
                    item.get("recommended_action", "")
                )[:400],
                "verification": "unverified",
                "publisher": original.get(
                    "publisher", ""
                ),
                "source_type": original.get(
                    "source_type", "news"
                )
            }

            cleaned.append(entry)

        unavailable = (
            category_success[category] == 0
            or analysis_failed
        )

        output[category + "_updates"] = cleaned

        output[category] = (
            cleaned[0]
            if cleaned
            else empty_update(unavailable)
        )

    # No automatic alerts without source verification.
    output["alerts"] = []
    output["alert_count"] = 0
    output["errors"] = errors
    output["source_success_count"] = success
    output["article_count"] = total
    output["category_source_success"] = category_success

    OUTPUT.write_text(
        json.dumps(
            output,
            indent=2,
            ensure_ascii=False
        ) + "\n"
    )

    titles = [
        normalize(item["title"])
        for group in articles.values()
        for item in group
    ]

    prior_seen = history.get("seen", [])
    if not isinstance(prior_seen, list):
        prior_seen = []

    history["seen"] = list(dict.fromkeys(
        prior_seen + titles
    ))[-1500:]

    history["latest"] = {
        category: output[category]
        for category in CATEGORIES
    }

    HISTORY.write_text(
        json.dumps(
            history,
            indent=2,
            ensure_ascii=False
        ) + "\n"
    )

    print(
        f"Meds Cafe intelligence updated: {now} "
        f"articles: {total} "
        f"sources: {success} "
        f"alerts: 0 "
        f"errors: {len(errors)} "
        f"status: {output['status']}"
    )


if __name__ == "__main__":
    main()
