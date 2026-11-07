import json
import os
import re
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime
from pathlib import Path
from openai import OpenAI

OUTPUT = Path('intel.json')
HISTORY = Path('intel_history.json')
MODEL = os.getenv('OPENAI_MODEL', 'gpt-4.1-mini')
MARKETS = ['Alpena', 'Gaylord', 'Rogers City', 'Cheboygan', 'Manistee', 'Lowell', 'Cedar Springs']
SEARCHES = {
    'michigan': [
        'Michigan cannabis SB 597 license cap legislation',
        'Michigan marijuana 24% wholesale tax court legislature',
        'Michigan CRA cannabis recall enforcement license suspension',
        'Michigan cannabis wholesale price supply market',
        'Michigan dispensary closure acquisition sale Alpena Gaylord Cheboygan',
        'Michigan dispensary opening closure Lowell Cedar Springs Manistee Rogers City',
    ],
    'federal': [
        'DEA marijuana rescheduling Schedule III 280E official',
        'Congress federal hemp THC law regulation',
        'DOJ DEA cannabis enforcement tax policy',
    ],
    'virginia': [
        'Virginia adult use cannabis retail licensing Cannabis Control Authority',
        'Virginia marijuana retail legalization legislation governor',
    ],
    'florida': [
        'Florida hemp THC law licensing enforcement',
        'Florida cannabis marijuana regulatory changes',
    ],
    'tennessee': [
        'Tennessee hemp THCA flower law licensing regulation',
        'Tennessee cannabis hemp enforcement legislation',
    ],
}


def load_json(path, default):
    try:
        return json.loads(path.read_text()) if path.exists() else default
    except (OSError, ValueError):
        return default


def fetch_rss(query):
    params = urllib.parse.urlencode({'q': query + ' when:7d', 'hl': 'en-US', 'gl': 'US', 'ceid': 'US:en'})
    req = urllib.request.Request('https://news.google.com/rss/search?' + params, headers={'User-Agent': 'Mozilla/5.0'})
    with urllib.request.urlopen(req, timeout=20) as response:
        root = ET.fromstring(response.read())
    results = []
    for item in root.findall('.//item')[:12]:
        title = item.findtext('title', '').strip()
        link = item.findtext('link', '').strip()
        published = item.findtext('pubDate', '')
        source = item.find('source')
        try:
            date = parsedate_to_datetime(published).astimezone(timezone.utc).isoformat()
        except (TypeError, ValueError, OverflowError):
            date = ''
        if title and link:
            results.append({'title': title, 'url': link, 'date': date, 'publisher': source.text if source is not None else ''})
    return results


def normalize(title):
    return re.sub(r'\W+', ' ', title.casefold()).strip()


def empty_update():
    return {'headline': 'No verified change', 'summary': '', 'impact': '', 'url': '', 'significant': False,
            'date': '', 'recommended_action': '', 'verification': 'unverified'}


def main():
    now = datetime.now(timezone.utc)
    history = load_json(HISTORY, {'seen': [], 'alerts': [], 'latest': {}})
    if not isinstance(history, dict):
        history = {'seen': [], 'alerts': [], 'latest': {}}
    prior_alerts = history.get('alerts', []) if isinstance(history.get('alerts'), list) else []
    prior_keys = {str(a.get('key', '')) for a in prior_alerts if isinstance(a, dict)}
    articles, errors = {}, []
    for category, queries in SEARCHES.items():
        unique = {}
        for query in queries:
            try:
                for item in fetch_rss(query):
                    unique.setdefault(normalize(item['title']), item)
            except Exception as exc:
                errors.append(f'{category}: {type(exc).__name__}: {exc}')
        articles[category] = list(unique.values())[:45]

    instructions = '''You are a cautious cannabis regulatory/business analyst for Meds Cafe.
Return ONLY a valid JSON object with keys michigan, federal, virginia, florida, tennessee.
Each value must be an ARRAY of up to 5 developments with fields:
headline, summary (<=45 words), impact (<=35 words), url, date, significant (boolean),
recommended_action (<=35 words), verification ('verified' or 'unverified').
Use ONLY supplied RSS metadata. Headlines are leads, not proof: without direct primary-source
text or corroborated evidence, verification MUST be 'unverified' and significant MUST be false.
Never assert a bill passed, law changed, facility closed, license issued, or acquisition is available
solely because a headline suggests it. Never invent source URLs or dates. If uncertain, omit.
Prefer developments relevant to Michigan locations Alpena, Gaylord, Rogers City, Cheboygan,
Manistee, Lowell, Cedar Springs, plus Virginia expansion, federal 280E/rescheduling, hemp,
wholesale pricing and acquisitions. Do not repeat essentially identical stories.
Empty array is correct when there are no useful leads. A recommendation must be actionable
and conditional when not verified.''' 
    payload = {'today_utc': now.isoformat(), 'articles': articles,
               'previous_alerts': prior_alerts[-100:]}
    client = OpenAI(api_key=os.environ['OPENAI_API_KEY'])
    response = client.chat.completions.create(
        model=MODEL, response_format={'type': 'json_object'}, temperature=0.1,
        messages=[{'role': 'system', 'content': instructions},
                  {'role': 'user', 'content': json.dumps(payload)}])
    parsed = json.loads(response.choices[0].message.content)
    if not isinstance(parsed, dict):
        raise ValueError('AI output must be an object')
    output = {'updated_at': now.isoformat(), 'status': 'partial' if errors else 'ok'}
    new_alerts = []
    for category in SEARCHES:
        raw = parsed.get(category, [])
        if isinstance(raw, dict):
            raw = [raw]
        if not isinstance(raw, list):
            raw = []
        approved_urls = {a['url'] for a in articles[category]}
        cleaned = []
        for item in raw[:5]:
            if not isinstance(item, dict) or item.get('url') not in approved_urls:
                continue
            verified = item.get('verification') == 'verified'
            # RSS metadata alone never establishes independent primary-source verification.
            verified = False
            entry = {
                'headline': str(item.get('headline', ''))[:220],
                'summary': str(item.get('summary', ''))[:500],
                'impact': str(item.get('impact', ''))[:400],
                'url': item['url'],
                'date': str(item.get('date', ''))[:40],
                'significant': False,
                'recommended_action': str(item.get('recommended_action', ''))[:400],
                'verification': 'unverified',
            }
            cleaned.append(entry)
            # No automatic alerts until a primary-source verification stage is added.
        output[category + '_updates'] = cleaned
        output[category] = cleaned[0] if cleaned else empty_update()
    output['alerts'] = new_alerts
    output['alert_count'] = 0
    output['errors'] = errors
    OUTPUT.write_text(json.dumps(output, indent=2, ensure_ascii=False) + '\n')
    titles = [normalize(a['title']) for group in articles.values() for a in group]
    history['seen'] = list(dict.fromkeys(history.get('seen', []) + titles))[-1500:]
    history['alerts'] = prior_alerts[-500:]
    history['latest'] = {k: output[k] for k in SEARCHES}
    HISTORY.write_text(json.dumps(history, indent=2, ensure_ascii=False) + '\n')
    print('Meds Cafe intelligence updated:', now.isoformat(), 'alerts:', len(new_alerts), 'errors:', len(errors))


if __name__ == '__main__':
    main()
