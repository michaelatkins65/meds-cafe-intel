
import os
import json
import re
import urllib.request
import urllib.parse
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

from openai import OpenAI

OUTPUT = Path("intel.json")
HISTORY = Path("intel_history.json")
MODEL = "gpt-4.1-mini"

SEARCHES = {
    "michigan": [
        "Michigan cannabis SB 597 licensing tax",
        "Michigan CRA marijuana recall enforcement",
        "Michigan cannabis wholesale prices dispensary closures",
        "Michigan cannabis dispensary acquisition sale",
    ],
    "federal": [
        "DEA marijuana rescheduling Schedule III 280E",
        "federal hemp THC legislation cannabis",
        "federal cannabis tax regulation enforcement",
    ],
}

def fetch_rss(query):
    params = urllib.parse.urlencode({
        "q": query + " when:3d",
        "hl": "en-US",
        "gl": "US",
        "ceid": "US:en",
    })

    url = (
        "https://news.google.com/rss/search?"
        + params
    )

    request = urllib.request.Request(
        url,
        headers={"User-Agent": "Mozilla/5.0"}
    )

    with urllib.request.urlopen(
        request, timeout=20
    ) as response:
        xml = response.read()

    root = ET.fromstring(xml)
    results = []

    for item in root.findall(".//item")[:12]:
        title = item.findtext("title", "")
        link = item.findtext("link", "")
        published = item.findtext("pubDate", "")

        try:
            date = parsedate_to_datetime(
                published
            ).isoformat()
        except Exception:
            date = ""

        results.append({
            "title": title,
            "url": link,
            "date": date,
        })

    return results

def load_json(path, fallback):
    if not path.exists():
        return fallback
    try:
        return json.loads(path.read_text())
    except Exception:
        return fallback

def main():
    now = datetime.now(timezone.utc)
    history = load_json(HISTORY, {
        "seen": [],
        "latest": {}
    })

    articles = {}
    errors = []

    for category, queries in SEARCHES.items():
        collected = []

        for query in queries:
            try:
                collected.extend(fetch_rss(query))
            except Exception as exc:
                errors.append(
                    f"{category}: {str(exc)}"
                )

        unique = {}
        for item in collected:
            key = item["title"].lower().strip()
            if key and key not in unique:
                unique[key] = item

        articles[category] = list(
            unique.values()
        )[:35]

    client = OpenAI(
        api_key=os.environ["OPENAI_API_KEY"]
    )

    prompt = {
        "today_utc": now.isoformat(),
        "previously_seen_titles": history.get(
            "seen", []
        )[-250:],
        "articles": articles,
    }

    instructions = """
You are a cautious business intelligence analyst
for Meds Cafe, a Michigan cannabis retailer.

Prioritize:
- Michigan SB 597, licensing and tax changes
- Michigan CRA enforcement and recalls
- Federal Schedule III, 280E and hemp policy
- Competitor openings and closures in Alpena,
  Gaylord, Rogers City, Cheboygan, Manistee,
  Lowell and Cedar Springs
- Wholesale prices, supply disruptions
- Actionable cannabis acquisition opportunities

Analyze only the supplied articles.
Do not invent facts or source URLs.
Do not claim a legal status changed based only
on a speculative headline.

A headline alone is NOT sufficient verification.
Treat findings as provisional unless the supplied
material contains adequate primary-source evidence.

Return valid JSON with this structure:
{
  "michigan": {
    "headline": "Short update or No verified change",
    "summary": "Maximum 30 words",
    "impact": "Maximum 20 words",
    "url": "Source URL or empty string",
    "significant": false
  },
  "federal": {
    "headline": "Short update or No verified change",
    "summary": "Maximum 30 words",
    "impact": "Maximum 20 words",
    "url": "Source URL or empty string",
    "significant": false
  }
}

Choose only the most important development
for each category.

If nothing reliable is found, say
"No verified change".

Do not label something significant without
credible evidence of a material business impact.
"""

    response = client.chat.completions.create(
        model=MODEL,
        response_format={
            "type": "json_object"
        },
        messages=[
            {
                "role": "system",
                "content": instructions
            },
            {
                "role": "user",
                "content": json.dumps(prompt)
            }
        ],
        temperature=0.1,
    )

    result = json.loads(
        response.choices[0].message.content
    )

    output = {
        "updated_at": now.isoformat(),
        "status": (
            "partial" if errors else "ok"
        ),
        "michigan": result.get(
            "michigan", {}
        ),
        "federal": result.get(
            "federal", {}
        ),
    }

    OUTPUT.write_text(
        json.dumps(output, indent=2)
    )

    all_titles = [
        item["title"]
        for group in articles.values()
        for item in group
    ]

    history["seen"] = list(dict.fromkeys(
        history.get("seen", []) + all_titles
    ))[-500:]

    HISTORY.write_text(
        json.dumps(history, indent=2)
    )

    print(
        "Meds Cafe intelligence updated:",
        now.isoformat()
    )

if __name__ == "__main__":
    main()
