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
from openai import OpenAI

OUTPUT = Path('intel.json')
HISTORY = Path('intel_history.json')
MODEL = os.getenv('OPENAI_MODEL', 'gpt-4.1-mini')
CATEGORIES = ['michigan', 'federal', 'virginia', 'florida', 'tennessee', 'texas']
SEARCHES = {
 'michigan': ['Michigan SB 597 cannabis license cap', 'Michigan marijuana wholesale tax 24 percent', 'Michigan CRA cannabis recall enforcement', 'Michigan cannabis wholesale pricing supply', 'Michigan dispensary closure opening acquisition', 'Alpena Gaylord Cheboygan Manistee Lowell Cedar Springs Rogers City cannabis'],
 'federal': ['DEA marijuana rescheduling Schedule III 280E', 'federal hemp THC legislation Congress', 'IRS cannabis 280E federal marijuana policy'],
 'virginia': ['Virginia adult use cannabis retail licensing', 'Virginia Cannabis Control Authority regulations', 'Virginia marijuana retail legalization governor'],
 'florida': ['Florida hemp THC retail regulation enforcement', 'Florida medical marijuana licensing dispensary acquisition'],
 'tennessee': ['Tennessee THCA hemp flower licensing enforcement', 'Tennessee intoxicating hemp legislation'],
 'texas': ['Texas cannabis medical marijuana licensing expansion', 'Texas hemp THC restrictions regulation', 'Texas 2027 marijuana reform legislature']
}
WATCH = {
 'michigan': [('SB 597 licensing limits', 'Monitor legislative movement and effects on license availability.'), ('24% wholesale cannabis tax', 'Watch implementation, legal challenges, and wholesale margin effects.'), ('CRA enforcement and local competitors', 'Watch recalls, store closures, openings, and acquisition opportunities.')],
 'federal': [('Schedule III and Section 280E', 'Monitor official rescheduling actions and tax implications.'), ('Federal hemp THC rules', 'Watch effective dates, enforcement, and retailer impacts.')],
 'virginia': [('Adult-use retail licensing', 'Watch legislation, licensing rules, and market-entry timing.')],
 'florida': [('Hemp THC retail restrictions', 'Watch enacted rules, effective dates, and enforcement.')],
 'tennessee': [('THCA and intoxicating hemp rules', 'Watch licensing, restrictions, and enforcement.')],
 'texas': [('Medical cannabis expansion and licensing', 'Watch license opportunities and program expansion.'), ('Hemp THC and 2027 reform', 'Watch regulatory changes and legislative proposals.')]
}
HEADERS = {'User-Agent': 'Mozilla/5.0 MedsCafeIntel/3.0', 'Accept': 'application/json, application/rss+xml, application/xml, */*'}

def load_json(path, default):
 try:
  value = json.loads(path.read_text())
  return value if isinstance(value, dict) else default
 except (OSError, ValueError):
  return default

def request_bytes(url, attempts=3):
 for i in range(attempts):
  try:
   with urllib.request.urlopen(urllib.request.Request(url, headers=HEADERS), timeout=25) as r:
    return r.read(2000000)
  except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError):
   if i == attempts-1: raise
   time.sleep(i+1)

def dt(value):
 if not value: return None
 try:
  d = datetime.fromisoformat(value.replace('Z', '+00:00'))
  return d.replace(tzinfo=timezone.utc) if d.tzinfo is None else d.astimezone(timezone.utc)
 except ValueError: return None

def normalize(text):
 return re.sub(r'\W+', ' ', str(text).casefold()).strip()

def parse_rss(raw):
 root = ET.fromstring(raw)
 result = []
 for item in root.findall('.//item')[:20]:
  title = (item.findtext('title') or '').strip()
  url = (item.findtext('link') or '').strip()
  source = item.find('source')
  try: date = parsedate_to_datetime(item.findtext('pubDate') or '').astimezone(timezone.utc).isoformat()
  except (TypeError, ValueError, OverflowError): continue
  if title and url:
   result.append({'title': title, 'url': url, 'date': date, 'publisher': source.text.strip() if source is not None and source.text else 'Google News', 'source_type': 'news'})
 return result

def fetch_google(query):
 params = urllib.parse.urlencode({'q': query+' when:3d', 'hl':'en-US', 'gl':'US', 'ceid':'US:en'})
 return parse_rss(request_bytes('https://news.google.com/rss/search?'+params))

def fetch_register(term):
 params = urllib.parse.urlencode({'conditions[term]':term, 'per_page':20, 'order':'newest'})
 payload = json.loads(request_bytes('https://www.federalregister.gov/api/v1/documents.json?'+params))
 return [{'title':x['title'], 'url':x['html_url'], 'date':x.get('publication_date',''), 'publisher':'Federal Register', 'source_type':'official_document'} for x in payload.get('results',[]) if x.get('title') and x.get('html_url')]

def fetch_all(now):
 groups = {c:[] for c in CATEGORIES}; errors=[]; success={c:0 for c in CATEGORIES}
 for cat in CATEGORIES:
  for q in SEARCHES[cat]:
   try:
    groups[cat].extend(fetch_google(q)); success[cat]+=1
   except Exception as e: errors.append(f'{cat}: {type(e).__name__}: {e}')
 for term in ['marijuana', 'cannabis', 'hemp tetrahydrocannabinol']:
  try: groups['federal'].extend(fetch_register(term)); success['federal']+=1
  except Exception as e: errors.append(f'Federal Register: {type(e).__name__}: {e}')
 cutoff = now-timedelta(hours=72)
 for cat in CATEGORIES:
  unique={}
  for a in groups[cat]:
   published=dt(a['date'])
   if not published or published < cutoff or published > now+timedelta(hours=2): continue
   key=normalize(a['title'])
   if key and (key not in unique or published > dt(unique[key]['date'])): unique[key]=a
  groups[cat]=sorted(unique.values(), key=lambda a:dt(a['date']), reverse=True)[:65]
 return groups, errors, success

def analyze(client, groups, now):
 instructions = '''You are an evidence-conscious cannabis business intelligence editor for Meds Cafe. Return JSON object with exactly six arrays: michigan, federal, virginia, florida, tennessee, texas. Each array up to 5 objects with headline, summary, impact, url, recommended_action. Use ONLY supplied source URLs, titles, and dates; never invent dates, legal outcomes, or events. Select high-value business developments for a multi-location Michigan retailer: licensing, SB 597, wholesale taxes, CRA enforcement, competitors, supply, acquisitions, federal rescheduling/280E, hemp rules, and expansion in Virginia/Texas. Prefer important recent developments over generic press releases. Only include a state-specific story if the headline clearly concerns that state; federal stories belong under federal unless direct state relevance is explicit. Do not put Missouri or Illinois news under Florida or Tennessee. Treat news as unverified reporting; do not claim a law has passed based on a headline. Avoid overlapping coverage of the same event across publishers. Return empty arrays rather than fillers. Rank by business importance, then recency.''' 
 response=client.chat.completions.create(model=MODEL, response_format={'type':'json_object'}, temperature=0.1, messages=[{'role':'system','content':instructions},{'role':'user','content':json.dumps({'today_utc':now.isoformat(),'articles':groups})}])
 result=json.loads(response.choices[0].message.content)
 return result if isinstance(result,dict) else {}

def empty():
 return {'headline':'No current intelligence leads','summary':'','impact':'','url':'','date':'','significant':False,'recommended_action':'','verification':'unverified'}

def main():
 now=datetime.now(timezone.utc)
 groups, errors, successes=fetch_all(now)
 total=sum(len(g) for g in groups.values())
 parsed={}
 if total:
  try: parsed=analyze(OpenAI(api_key=os.environ['OPENAI_API_KEY']),groups,now)
  except Exception as e: errors.append(f'AI analysis: {type(e).__name__}: {e}')
 output={'updated_at':now.isoformat(),'status':'unavailable' if not total else 'partial' if errors else 'ok'}
 for cat in CATEGORIES:
  allowed={a['url']:a for a in groups[cat]}
  raw=parsed.get(cat,[])
  if not isinstance(raw,list): raw=[]
  cleaned=[]; used=set()
  for item in raw:
   if not isinstance(item,dict): continue
   url=item.get('url','')
   if url not in allowed or url in used: continue
   used.add(url); source=allowed[url]
   headline=str(item.get('headline') or source['title'])[:220]
   cleaned.append({'headline':headline,'summary':str(item.get('summary',''))[:500], 'impact':str(item.get('impact',''))[:400], 'url':url, 'date':source['date'], 'significant':False, 'recommended_action':str(item.get('recommended_action',''))[:400], 'verification':'unverified', 'publisher':source['publisher'], 'source_type':source['source_type'], 'display_type':'new'})
   if len(cleaned)>=5: break
  output[cat+'_updates']=cleaned
  output[cat]=cleaned[0] if cleaned else empty()
  output[cat+'_watching']=[{'headline':title,'summary':detail,'impact':detail,'url':'','display_type':'watching'} for title,detail in WATCH[cat]]
 output.update({'alerts':[],'alert_count':0,'errors':errors,'source_success_count':sum(successes.values()),'article_count':total,'category_source_success':successes})
 OUTPUT.write_text(json.dumps(output,indent=2,ensure_ascii=False)+'\n')
 history=load_json(HISTORY,{'seen':[],'alerts':[],'latest':{}})
 prior=history.get('seen',[])
 if not isinstance(prior,list): prior=[]
 history['seen']=list(dict.fromkeys(prior+[normalize(a['title']) for group in groups.values() for a in group]))[-1500:]
 history['latest']={cat:output[cat] for cat in CATEGORIES}
 HISTORY.write_text(json.dumps(history,indent=2,ensure_ascii=False)+'\n')
 print(f'Meds Cafe: {total} articles, {sum(successes.values())} sources, {len(errors)} errors, {output["status"]}')

if __name__ == '__main__':
 main()
