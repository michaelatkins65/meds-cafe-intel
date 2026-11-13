import json
import os
import re
import time
import urllib.parse
import urllib.request
import urllib.error
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime
from pathlib import Path
from difflib import SequenceMatcher
from openai import OpenAI

OUTPUT = Path('intel.json')
HISTORY = Path('intel_history.json')
MODEL = os.getenv('OPENAI_MODEL', 'gpt-4.1-mini')
CATEGORIES = ['michigan', 'federal', 'virginia', 'florida', 'tennessee', 'texas']
FRESH_HOURS = 72
MAX_PER_CATEGORY = 5
SEARCHES = {
 'michigan': ['Michigan SB 597 cannabis license cap', 'Michigan marijuana 24 percent wholesale tax', 'Michigan CRA cannabis recall enforcement', 'Michigan cannabis wholesale prices supply', 'Michigan dispensary license suspension closure', 'Alpena Gaylord Cheboygan dispensary opening closing', 'Lowell Cedar Springs Manistee Rogers City cannabis dispensary', 'Michigan cannabis dispensary business for sale acquisition', 'site:michigan.gov/cra cannabis recall', 'site:legislature.mi.gov SB 597 marijuana'],
 'federal': ['DEA marijuana rescheduling Schedule III 280E', 'federal cannabis rescheduling official decision', 'Congress hemp THC legislation', 'IRS marijuana 280E cannabis tax', 'federal cannabis enforcement policy'],
 'virginia': ['Virginia adult use cannabis retail licensing', 'Virginia Cannabis Control Authority regulations', 'Virginia marijuana retail legalization governor'],
 'florida': ['Florida hemp THC law licensing enforcement', 'Florida hemp retail restrictions legislation'],
 'tennessee': ['Tennessee THCA hemp flower law licensing', 'Tennessee intoxicating hemp enforcement'],
 'texas': ['Texas medical cannabis expansion dispensary licensing', 'Texas compassionate use program cannabis licenses', 'Texas THC hemp restrictions lawsuit enforcement', 'Texas cannabis legalization legislation']
}
HEADERS = {'User-Agent': 'Mozilla/5.0 MedsCafeIntel/5.0', 'Accept': 'application/json, application/rss+xml, application/xml, */*'}
STATE_TERMS = {
 'michigan': ['michigan', 'alpena', 'gaylord', 'cheboygan', 'lowell', 'cedar springs', 'manistee', 'rogers city', 'cra'],
 'virginia': ['virginia', 'richmond', 'henrico', 'newport news', 'virginian'],
 'florida': ['florida', 'fluent', 'tallahassee', 'miami', 'orlando', 'tampa', 'key west'],
 'tennessee': ['tennessee', 'nashville', 'knoxville', 'memphis', 'chattanooga'],
 'texas': ['texas', 'austin', 'dallas', 'houston', 'san antonio', 'texan']
}
OTHER_STATES = ['alabama', 'alaska', 'arizona', 'arkansas', 'california', 'colorado', 'connecticut', 'delaware', 'georgia', 'hawaii', 'idaho', 'illinois', 'indiana', 'iowa', 'kansas', 'kentucky', 'louisiana', 'maine', 'maryland', 'massachusetts', 'minnesota', 'mississippi', 'missouri', 'montana', 'nebraska', 'nevada', 'new hampshire', 'new jersey', 'new mexico', 'new york', 'north carolina', 'north dakota', 'ohio', 'oklahoma', 'oregon', 'pennsylvania', 'rhode island', 'south carolina', 'south dakota', 'utah', 'vermont', 'washington', 'west virginia', 'wisconsin', 'wyoming']
PRIORITY = ['sb 597', 'license cap', '24 percent', '24%', 'wholesale tax', 'reschedul', 'schedule iii', '280e', 'licens', 'enforcement', 'recall', 'dispensary', 'retail', 'acquisition', 'hemp', 'thc', 'wholesale']

def load_json(path, default):
 try:
  value = json.loads(path.read_text(encoding='utf-8'))
  return value if isinstance(value, dict) else default
 except (OSError, ValueError):
  return default

def request_bytes(url, attempts=3):
 last_error = None
 for attempt in range(attempts):
  try:
   req = urllib.request.Request(url, headers=HEADERS)
   with urllib.request.urlopen(req, timeout=25) as response:
    return response.read(2000000)
  except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError) as exc:
   last_error = exc
   if attempt + 1 < attempts:
    time.sleep(2 * (attempt + 1))
 raise last_error

def normalize(value):
 return re.sub(r'\W+', ' ', str(value).casefold()).strip()

def parse_date(value):
 if not value:
  return None
 try:
  d = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
 except ValueError:
  try:
   d = parsedate_to_datetime(value)
  except (TypeError, ValueError, OverflowError):
   return None
 if d.tzinfo is None:
  d = d.replace(tzinfo=timezone.utc)
 return d.astimezone(timezone.utc)

def parse_rss(data):
 root = ET.fromstring(data)
 results = []
 for item in root.findall('.//item')[:20]:
  title = (item.findtext('title') or '').strip()
  link = (item.findtext('link') or '').strip()
  date = parse_date(item.findtext('pubDate') or '')
  source = item.find('source')
  publisher = source.text.strip() if source is not None and source.text else 'Google News'
  if title and link:
   results.append({'title': title, 'url': link, 'date': date.isoformat() if date else '', 'publisher': publisher, 'source_type': 'news', 'description': ''})
 return results

def fetch_google_news(query):
 params = urllib.parse.urlencode({'q': query + ' when:7d', 'hl': 'en-US', 'gl': 'US', 'ceid': 'US:en'})
 return parse_rss(request_bytes('https://news.google.com/rss/search?' + params))

def fetch_federal_register(term):
 params = urllib.parse.urlencode({'conditions[term]': term, 'per_page': 20, 'order': 'newest'})
 data = json.loads(request_bytes('https://www.federalregister.gov/api/v1/documents.json?' + params))
 results = []
 for item in data.get('results', []):
  title, link = item.get('title', ''), item.get('html_url', '')
  if title and link:
   results.append({'title': title, 'url': link, 'date': item.get('publication_date', ''), 'publisher': 'Federal Register', 'source_type': 'official_document', 'description': (item.get('abstract') or '')[:700]})
 return results

def relevant_geography(category, item):
 if category == 'federal':
  return True
 title = normalize(item['title'])
 matches = any(re.search(r'\b' + re.escape(term) + r'\b', title) for term in STATE_TERMS[category])
 if not matches:
  return False
 # Reject a different state's local story even if a query matched it indirectly.
 if any(re.search(r'\b' + re.escape(state) + r'\b', title) for state in OTHER_STATES):
  return False
 return True

def freshness(item, now):
 d = parse_date(item.get('date'))
 return d is not None and now - timedelta(hours=FRESH_HOURS) <= d <= now + timedelta(hours=2)

def priority(item, now):
 title = normalize(item['title'])
 date = parse_date(item.get('date')) or now - timedelta(days=30)
 age_hours = max(0, (now - date).total_seconds() / 3600)
 relevance = sum(2 if word in title else 0 for word in PRIORITY)
 return relevance + (FRESH_HOURS - age_hours) / 12 + (2 if item.get('source_type') == 'official_document' else 0)

def dedupe(items):
 result = []
 for item in items:
  title = normalize(item['title'].rsplit(' - ', 1)[0])
  if any(SequenceMatcher(None, title, normalize(other['title'].rsplit(' - ', 1)[0])).ratio() > .79 for other in result):
   continue
  result.append(item)
 return result

def fetch_all(now):
 articles = {c: [] for c in CATEGORIES}
 errors = []
 success = 0
 category_success = {c: 0 for c in CATEGORIES}
 for category in CATEGORIES:
  for query in SEARCHES[category]:
   try:
    articles[category].extend(fetch_google_news(query))
    success += 1
    category_success[category] += 1
   except Exception as exc:
    errors.append(f'{category} Google News: {type(exc).__name__}: {exc}')
 for term in ['marijuana', 'cannabis', 'hemp tetrahydrocannabinol']:
  try:
   articles['federal'].extend(fetch_federal_register(term))
   success += 1
   category_success['federal'] += 1
  except Exception as exc:
   errors.append(f'Federal Register {term}: {type(exc).__name__}: {exc}')
 for category in CATEGORIES:
  eligible = [x for x in articles[category] if freshness(x, now) and relevant_geography(category, x)]
  eligible.sort(key=lambda x: priority(x, now), reverse=True)
  articles[category] = dedupe(eligible)[:45]
 return articles, errors, success, category_success

def analyze(client, articles, now):
 instructions = '''You are a cautious business intelligence analyst for Meds Cafe, a Michigan cannabis retailer.
Return ONLY valid JSON with six arrays: michigan, federal, virginia, florida, tennessee, texas.
Each array may contain up to five items, each with headline, summary, impact, url, date, recommended_action.
Use only supplied article facts, URLs and dates. Do not fabricate legal outcomes or source verification.
Prefer concrete regulatory and commercial developments over general commentary. Michigan: SB 597, license caps, wholesale tax, CRA enforcement, store competition, acquisitions and pricing. Federal: rescheduling, 280E, hemp. Virginia and Texas: retail licensing and market entry. Florida and Tennessee: hemp rules.
Do not describe a bill, proposal, lawsuit or headline as enacted law without evidence. Do not invent dates or implications. Avoid repeating the same event through different publishers. Keep the strongest stories first. Return empty arrays where nothing useful exists.'''
 response = client.chat.completions.create(model=MODEL, response_format={'type': 'json_object'}, temperature=0.1, messages=[{'role': 'system', 'content': instructions}, {'role': 'user', 'content': json.dumps({'today_utc': now.isoformat(), 'articles': articles})}])
 result = json.loads(response.choices[0].message.content)
 return result if isinstance(result, dict) else {}

def empty_update(unavailable=False):
 return {'headline': 'Research unavailable' if unavailable else 'No verified change', 'summary': '', 'impact': '', 'url': '', 'significant': False, 'date': '', 'recommended_action': '', 'verification': 'unverified'}

def main():
 now = datetime.now(timezone.utc)
 history = load_json(HISTORY, {'seen': [], 'alerts': [], 'latest': {}})
 articles, errors, success, category_success = fetch_all(now)
 total = sum(map(len, articles.values()))
 parsed = {}
 analysis_failed = False
 if total:
  try:
   client = OpenAI(api_key=os.environ['OPENAI_API_KEY'])
   parsed = analyze(client, articles, now)
  except Exception as exc:
   analysis_failed = True
   errors.append(f'AI analysis: {type(exc).__name__}: {exc}')
 output = {'updated_at': now.isoformat(), 'status': 'unavailable' if total == 0 or analysis_failed else 'partial' if errors else 'ok'}
 for category in CATEGORIES:
  allowed = {x['url']: x for x in articles[category]}
  raw = parsed.get(category, [])
  if not isinstance(raw, list):
   raw = []
  cleaned = []
  for item in raw:
   if not isinstance(item, dict) or item.get('url') not in allowed:
    continue
   original = allowed[item['url']]
   headline = str(item.get('headline') or original['title'])[:220]
   cleaned.append({'headline': headline, 'summary': str(item.get('summary', ''))[:500], 'impact': str(item.get('impact', ''))[:400], 'url': original['url'], 'date': original['date'], 'significant': False, 'recommended_action': str(item.get('recommended_action', ''))[:400], 'verification': 'unverified', 'publisher': original.get('publisher', ''), 'source_type': original.get('source_type', 'news'), '_score': priority(original, now)})
  cleaned.sort(key=lambda x: (x['_score'], x['date']), reverse=True)
  unique = []
  for entry in cleaned:
   key = normalize(entry['headline'].rsplit(' - ', 1)[0])
   if any(SequenceMatcher(None, key, normalize(old['headline'].rsplit(' - ', 1)[0])).ratio() > .79 for old in unique):
    continue
   entry.pop('_score', None)
   unique.append(entry)
   if len(unique) == MAX_PER_CATEGORY:
    break
  output[category + '_updates'] = unique
  output[category] = unique[0] if unique else empty_update(category_success[category] == 0 or analysis_failed)
 output.update({'alerts': [], 'alert_count': 0, 'errors': errors, 'source_success_count': success, 'article_count': total, 'category_source_success': category_success})
 OUTPUT.write_text(json.dumps(output, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')
 titles = [normalize(item['title']) for group in articles.values() for item in group]
 prior = history.get('seen', [])
 if not isinstance(prior, list):
  prior = []
 history['seen'] = list(dict.fromkeys(prior + titles))[-1500:]
 history['latest'] = {c: output[c] for c in CATEGORIES}
 HISTORY.write_text(json.dumps(history, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')
 print(f'Meds Cafe intelligence updated: {now.isoformat()} articles: {total} sources: {success} alerts: 0 errors: {len(errors)} status: {output["status"]}')

if __name__ == '__main__':
 main()
