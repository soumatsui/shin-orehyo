import json
import os
import re
import time
from datetime import datetime, date, timedelta
from concurrent.futures import ThreadPoolExecutor
from urllib.request import Request, urlopen

from bs4 import BeautifulSoup

USER_AGENT = (
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
    'AppleWebKit/537.36 (KHTML, like Gecko) '
    'Chrome/128.0.0.0 Safari/537.36'
)
BASE_URL = 'https://www.boatrace.jp/owpc/pc/race'


# ============================================================================
# JMA TIDE DATA INTEGRATION
# ----------------------------------------------------------------------------
# Design notes for future AI/code maintainers:
# 1) Tide data is fetched ONCE per stadium/day, never once per race.
# 2) The venue -> JMA station relationship lives in config/tide_stations.json.
# 3) Disabled/unavailable mappings produce NO "tide" property in JSON.
# 4) JMA values are astronomical tide predictions ("天文潮位"), not observed
#    water levels. Do not silently treat them as actual water levels.
# 5) A proxy station is tagged in the config. Prediction code should be able
#    to ignore low-confidence mappings later without rewriting the scraper.
# ============================================================================

JMA_TIDE_BASE_URL = 'https://www.data.jma.go.jp/kaiyou/db/tide/suisan/suisan.php'
TIDE_CONFIG_PATH = os.path.join('config', 'tide_stations.json')


def load_tide_station_config(path: str = TIDE_CONFIG_PATH) -> dict:
    """Load venue -> JMA tide station mapping.

    Keeping this mapping in JSON makes it easy for another AI/developer to
    correct a station choice without touching scraping logic.
    """
    try:
        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception as exc:
        print(f'JMA潮汐マッピング読込失敗: {exc}')
        return {}


def _jma_tide_cache_path(today: str, station_code: str) -> str:
    """One cache file per venue-day/station.

    The cache prevents 12 race requests (or repeated workflow executions)
    from hammering JMA for identical daily data.
    """
    cache_dir = os.path.join('data', 'tide')
    os.makedirs(cache_dir, exist_ok=True)
    return os.path.join(cache_dir, f'{today}_{station_code}.json')


def _parse_jma_tide_row(row, target_date: str):
    """Parse one JMA tide-table row.

    JMA's HTML table has multiple fixed tide slots. The parser intentionally
    looks for the date first, then pairs a time cell with the following level
    cell. This is more resilient than relying on CSS classes that can change.
    The first four pairs are high-tide slots; the last four are low-tide slots.
    """
    cells = [clean_text(td.get_text(' ', strip=True)) for td in row.find_all('td')]
    if not cells:
        return None

    if not any(target_date in c for c in cells):
        return None

    # Find the first time-looking cell; earlier cells can contain moon icons.
    first_time_idx = None
    for idx, cell in enumerate(cells):
        if re.fullmatch(r'\d{1,2}:\d{2}', cell):
            first_time_idx = idx
            break

    if first_time_idx is None:
        return None

    pairs = []
    idx = first_time_idx
    while idx + 1 < len(cells) and len(pairs) < 8:
        time_text = cells[idx]
        level_text = cells[idx + 1]
        if re.fullmatch(r'\d{1,2}:\d{2}', time_text):
            level_match = re.fullmatch(r'-?\d+(?:\.\d+)?', level_text)
            if level_match:
                pairs.append({
                    'time': time_text,
                    'level_cm': float(level_match.group())
                })
            else:
                # Keep position stable for '*' / missing cells.
                pairs.append(None)
        else:
            pairs.append(None)
        idx += 2

    highs = [p for p in pairs[:4] if p is not None]
    lows = [p for p in pairs[4:8] if p is not None]

    # Determine the lunar/tide label when it is directly present in row text.
    row_text = clean_text(row.get_text(' ', strip=True))
    tide_name = None
    # JMA primarily communicates tide size through moon-phase context. Keep
    # this nullable; do not invent "大潮/中潮/..." from incomplete evidence.
    for candidate in ('大潮', '中潮', '小潮', '長潮', '若潮'):
        if candidate in row_text:
            tide_name = candidate
            break

    if not highs and not lows:
        return None

    result = {
        'date': target_date,
        'high_tides': highs,
        'low_tides': lows,
    }
    if tide_name:
        result['tide_name'] = tide_name
    return result


def fetch_jma_tide_for_date(station_code: str, today: str):
    """Fetch the JMA astronomical tide prediction for one calendar day.

    We request a <=15-day window around the target date because JMA's web
    tide table supports multi-day periods. This keeps one HTTP request per
    venue/day while making the parser less sensitive to month boundaries.
    """
    target = datetime.strptime(today, '%Y%m%d').date()
    start = max(target.replace(day=1), target - timedelta(days=7))
    # Keep the period inside the same month for the JMA query.
    next_month = (target.replace(day=28) + timedelta(days=4)).replace(day=1)
    last_day = next_month - timedelta(days=1)
    end = min(last_day, target + timedelta(days=7))

    params = {
        'stn': station_code,
        'ye': target.year,
        'ys': target.year,
        'me': target.month,
        'ms': start.month,
        'ds': start.day,
        'de': end.day,
        'LV': 'DL',
        'S_HILO': 'on',
    }
    query = '&'.join(f'{k}={v}' for k, v in params.items())
    url = f'{JMA_TIDE_BASE_URL}?{query}'

    req = Request(
        url,
        headers={
            'User-Agent': USER_AGENT,
            'Accept-Language': 'ja,en;q=0.8',
        }
    )

    with urlopen(req, timeout=12) as response:
        html = response.read().decode('utf-8', errors='replace')

    soup = BeautifulSoup(html, 'html.parser')
    target_date_slash = target.strftime('%Y/%m/%d')

    for row in soup.select('tr'):
        parsed = _parse_jma_tide_row(row, target_date_slash)
        if parsed:
            parsed['source'] = 'JMA'
            parsed['source_url'] = url
            parsed['station_code'] = station_code
            parsed['retrieved_at'] = datetime.now().isoformat(timespec='seconds')
            return parsed

    return None


def get_daily_tide(jcd: str, today: str, station_config: dict):
    """Return cached JMA tide information for one stadium/day.

    Failure policy:
    - freshwater / disabled mapping -> None
    - JMA request/parser failure -> None
    - successful fetch -> compact tide object
    Never raise a fatal exception merely because tide data is unavailable.
    """
    cfg = station_config.get(jcd, {})
    if not cfg.get('enabled') or not cfg.get('station_code'):
        return None

    station_code = cfg['station_code']
    cache_path = _jma_tide_cache_path(today, station_code)

    try:
        if os.path.exists(cache_path):
            with open(cache_path, 'r', encoding='utf-8') as f:
                return json.load(f)
    except Exception as exc:
        print(f'潮汐キャッシュ読込失敗 {jcd}/{station_code}: {exc}')

    try:
        tide = fetch_jma_tide_for_date(station_code, today)
        if not tide:
            print(f'潮汐データなし JCD={jcd} station={station_code}')
            return None

        # Keep mapping provenance so future analytics can filter proxies.
        tide['mapping'] = {
            'stadium': cfg.get('stadium'),
            'station_name': cfg.get('station_name'),
            'confidence': cfg.get('confidence'),
        }

        with open(cache_path, 'w', encoding='utf-8') as f:
            json.dump(tide, f, ensure_ascii=False, indent=2)

        return tide

    except Exception as exc:
        print(f'JMA潮汐取得失敗 JCD={jcd} station={station_code}: {exc}')
        return None


def fetch_html(path: str, params: dict, timeout: int = 12) -> str:
    query = '&'.join(f'{k}={v}' for k, v in params.items())
    url = f'{BASE_URL}/{path}?{query}'
    req = Request(url, headers={'User-Agent': USER_AGENT})
    with urlopen(req, timeout=timeout) as response:
        return response.read().decode('utf-8', errors='replace')


def clean_text(value) -> str:
    if value is None:
        return ''
    return re.sub(r'\s+', ' ', str(value).replace('\xa0', ' ')).strip()


def safe_float(value):
    if value is None:
        return None
    text = clean_text(value).replace('kg', '').replace('m', '')
    text = text.replace('Ｆ', 'F').replace('Ｌ', 'L')
    match = re.search(r'-?\d+(?:\.\d+)?', text)
    return float(match.group()) if match else None


def safe_int(value):
    if value is None:
        return None
    match = re.search(r'\d+', clean_text(value))
    return int(match.group()) if match else None


def parse_rate_triplet(cell_text: str):
    nums = re.findall(r'\d+(?:\.\d+)?', clean_text(cell_text))
    if len(nums) >= 3:
        return [float(nums[0]), float(nums[1]), float(nums[2])]
    if len(nums) == 2:
        return [float(nums[0]), float(nums[1]), None]
    if len(nums) == 1:
        return [float(nums[0]), None, None]
    return [None, None, None]


def _default_racer(boat_no: int):
    """Create a complete racer object with explicit missing-value fields.

    Missing data is represented by None so downstream code can distinguish
    "not available" from a real zero. The UI/prediction layer should simply
    ignore unavailable fields rather than inventing values.
    """
    return {
        'no': boat_no,
        'name': f'選手{boat_no}号艇',
        'class': 'A1',
        'racer_no': None,
        'weight': None,
        'national': {'win_rate': None, 'top2_rate': None, 'top3_rate': None},
        'local': {'win_rate': None, 'top2_rate': None, 'top3_rate': None},
        'motor': {'no': None, 'top2_rate': None, 'top3_rate': None},
        'boat': {'no': None, 'top2_rate': None, 'top3_rate': None},
        'avg_st': None,
        'f_count': None,
        'l_count': None,
    }


def _extract_stat_block(text: str, item: dict):
    """Parse the stable race-card statistic block from one racer row.

    Official BOAT RACE row order (as exposed in the current racelist HTML):
      F / L / average ST /
      national win/2-rate/3-rate /
      local win/2-rate/3-rate /
      motor no/2-rate/3-rate /
      boat no/2-rate/3-rate

    The regex is intentionally anchored to F/L and the following numeric
    sequence. This avoids accidentally reading unrelated numbers such as
    recent-race results from the same row.
    """
    m = re.search(
        r'F\s*(?P<f>\d+)\s*'
        r'L\s*(?P<l>\d+)\s*'
        r'(?P<avg>\d+\.\d{2})\s+'
        r'(?P<nw>\d+\.\d{2})\s+(?P<n2>\d+\.\d{2})\s+(?P<n3>\d+\.\d{2})\s+'
        r'(?P<lw>\d+\.\d{2})\s+(?P<l2>\d+\.\d{2})\s+(?P<l3>\d+\.\d{2})\s+'
        r'(?P<mno>\d+)\s+(?P<m2>\d+\.\d{2})\s+(?P<m3>\d+\.\d{2})\s+'
        r'(?P<bno>\d+)\s+(?P<b2>\d+\.\d{2})\s+(?P<b3>\d+\.\d{2})',
        clean_text(text)
    )
    if not m:
        return False

    g = m.groupdict()
    item['f_count'] = int(g['f'])
    item['l_count'] = int(g['l'])
    item['avg_st'] = float(g['avg'])
    item['national'] = {
        'win_rate': float(g['nw']),
        'top2_rate': float(g['n2']),
        'top3_rate': float(g['n3']),
    }
    item['local'] = {
        'win_rate': float(g['lw']),
        'top2_rate': float(g['l2']),
        'top3_rate': float(g['l3']),
    }
    item['motor'] = {
        'no': int(g['mno']),
        'top2_rate': float(g['m2']),
        'top3_rate': float(g['m3']),
    }
    item['boat'] = {
        'no': int(g['bno']),
        'top2_rate': float(g['b2']),
        'top3_rate': float(g['b3']),
    }
    return True


def _extract_name_from_row(row):
    """Return the racer's display name text from an already-matched row.

    ROOT CAUSE OF THE MAPPING BUG (2026-09):
    A racer's profile link (href*="racersearch/profile?toban=...") appears
    TWICE inside one racer row: once wrapping the racer's photo <img> (text
    is empty) and once wrapping the racer's name text. Earlier code picked
    "the first matching link for this toban" without checking whether that
    link actually had visible text. When the photo link happened to come
    first in DOM order, item['name'] was set to '' and silently fell back
    to the '選手N号艇' placeholder -- even though racer_no, class, and all
    stat fields (which are parsed from the row's full text, not from the
    link) were already correct. This produced exactly the symptom reported:
    JSON name is a placeholder while _raw_cells clearly contains the real
    name for the same racer_no.

    Fix: within the confirmed correct <tr>, explicitly scan every profile
    link and use the first one whose extracted text is non-empty. Never
    assume link order.
    """
    for link in row.select('a[href*="racersearch/profile?toban="]'):
        text = clean_text(link.get_text(' ', strip=True))
        if text:
            return text
    return ''


def _extract_boat_no_from_row(row):
    """Extract the actual boat number from the first table cell.

    Using the first cell is safer than searching the entire row for "1-6",
    because the row also contains previous-race course/results numbers.
    """
    cells = row.find_all('td')
    if not cells:
        return None
    first = clean_text(cells[0].get_text(' ', strip=True))
    m = re.fullmatch(r'[1-6]', first)
    if m:
        return int(m.group())
    # A small fallback for markup such as "１" / surrounding whitespace.
    normalized = first.translate(str.maketrans('１２３４５６', '123456'))
    return int(normalized) if normalized in {'1', '2', '3', '4', '5', '6'} else None


def _choose_racer_row(link):
    """Choose the best parent <tr> for a racer profile link.

    BOAT RACE uses responsive/duplicate markup in places. A link can appear
    more than once. Prefer the row that contains the full stable statistic
    block rather than taking the first DOM occurrence blindly.
    """
    candidates = []
    node = link
    for _ in range(4):
        node = node.parent if node is not None else None
        if node is None:
            break
        if getattr(node, 'name', None) != 'tr':
            continue
        text = clean_text(node.get_text(' ', strip=True))
        score = 0
        if re.search(r'F\s*\d+\s*L\s*\d+', text):
            score += 5
        if re.search(r'\d+\.\d{2}\s+\d+\.\d{2}\s+\d+\.\d{2}', text):
            score += 3
        if len(node.find_all('td')) >= 8:
            score += 2
        candidates.append((score, node))

    if not candidates:
        return None
    candidates.sort(key=lambda x: x[0], reverse=True)
    return candidates[0][1]


def parse_race_card(jcd: str, r_idx: int, today: str):
    """Parse all six racers from the official BOAT RACE racelist.

    IMPORTANT FIX:
    The previous implementation selected rows by CSS class + any digit 1-6.
    That was fragile because the official page contains repeated responsive
    rows and historical-course numbers. It caused some boats (especially 1/2)
    to become placeholder records while other boats parsed correctly.

    New strategy:
      1. Find racer profile links (stable unique identifier = racer/toban).
      2. Choose the best parent <tr> containing the stable stats block.
      3. Read the boat number from the FIRST cell of that row.
      4. Parse the complete stat block from that row.
      5. Deduplicate by racer_no and/or boat_no.
    """
    soup = BeautifulSoup(
        fetch_html('racelist', {'rno': r_idx, 'jcd': jcd, 'hd': today}),
        'html.parser'
    )

    # Group profile links by racer ID to avoid responsive duplicate markup.
    links_by_toban = {}
    for link in soup.select('a[href*="racersearch/profile?toban="]'):
        href = link.get('href', '')
        m = re.search(r'toban=(\d+)', href)
        if not m:
            continue
        links_by_toban.setdefault(m.group(1), []).append(link)

    parsed = {}
    used_tobans = set()

    for toban, links in links_by_toban.items():
        best_item = None

        for link in links:
            row = _choose_racer_row(link)
            if row is None:
                continue

            boat_no = _extract_boat_no_from_row(row)
            if boat_no is None or boat_no in parsed:
                continue

            text = clean_text(row.get_text(' ', strip=True))
            cells = [clean_text(td.get_text(' ', strip=True)) for td in row.find_all('td')]
            item = _default_racer(boat_no)

            # NOTE: do not use `link` (the specific <a> we happened to
            # iterate to) for the name -- it may be the photo-only link.
            # Re-scan the confirmed row for the link that actually has text.
            item['name'] = _extract_name_from_row(row) or item['name']
            item['racer_no'] = int(toban)
            item['class'] = next(
                (c for c in ('A1', 'A2', 'B1', 'B2') if re.search(rf'\b{c}\b', text)),
                'A1'
            )

            # Weight is present in the racer identity cell as "xx.xkg".
            weight_match = re.search(r'(\d+(?:\.\d+)?)kg', text)
            if weight_match:
                item['weight'] = float(weight_match.group(1))

            _extract_stat_block(text, item)
            item['_raw_cells'] = cells

            # Keep a compact audit trail. It helps another AI diagnose a future
            # markup change without requiring the entire HTML source.
            item['_source'] = 'boatrace_official_racelist'

            parsed[boat_no] = item
            used_tobans.add(toban)
            best_item = item
            break

    # If responsive markup prevented a direct link->row match, perform a
    # second pass over every <tr> that contains a racer profile link.
    if len(parsed) < 6:
        for row in soup.find_all('tr'):
            boat_no = _extract_boat_no_from_row(row)
            if boat_no is None or boat_no in parsed:
                continue
            link = row.select_one('a[href*="racersearch/profile?toban="]')
            if link is None:
                continue
            href = link.get('href', '')
            m = re.search(r'toban=(\d+)', href)
            if not m or m.group(1) in used_tobans:
                continue

            text = clean_text(row.get_text(' ', strip=True))
            item = _default_racer(boat_no)
            # Same fix as the first pass: use the text-bearing profile link
            # in this row, not whichever link `row.select_one(...)` happened
            # to return first (which can be the photo-only link).
            item['name'] = _extract_name_from_row(row) or item['name']
            item['racer_no'] = int(m.group(1))
            item['class'] = next(
                (c for c in ('A1', 'A2', 'B1', 'B2') if re.search(rf'\b{c}\b', text)),
                'A1'
            )
            weight_match = re.search(r'(\d+(?:\.\d+)?)kg', text)
            if weight_match:
                item['weight'] = float(weight_match.group(1))
            _extract_stat_block(text, item)
            item['_raw_cells'] = [clean_text(td.get_text(' ', strip=True)) for td in row.find_all('td')]
            item['_source'] = 'boatrace_official_racelist'
            parsed[boat_no] = item
            used_tobans.add(m.group(1))

    racers = []
    for boat_no in range(1, 7):
        racers.append(parsed.get(boat_no, _default_racer(boat_no)))

    parsed_count = sum(1 for r in racers if r.get('racer_no') is not None)
    print(f'  racelist jcd={jcd} r={r_idx}: parsed {parsed_count}/6 racers')

    return {
        'race_no': r_idx,
        'racers': racers,
    }


def _extract_beforeinfo_row(row):
    """Extract stable pre-race fields from one official before-info row.

    Current official column order is:
      boat | photo | racer | weight | exhibition time | tilt | propeller |
      parts exchange | previous result ...

    We use cell-local parsing rather than scanning every number in the full
    row. The old number-scan approach could mistake a previous-race number
    such as "5R" for an exhibition time; this was visible in the generated
    JSON as 5.00 for boats 5/6.
    """
    cells = row.find_all('td')
    if not cells:
        return None

    boat_no = _extract_boat_no_from_row(row)
    if boat_no is None:
        return None

    texts = [clean_text(td.get_text(' ', strip=True)) for td in cells]
    joined = ' '.join(texts)

    data = {
        'boat_no': boat_no,
        'weight': None,
        'adjust_weight': None,
        'exhibition_time': None,
        'tilt': None,
        'propeller_changed': False,
        'parts_exchange': [],
    }

    # Weight: use a cell explicitly containing kg.
    for txt in texts:
        m = re.search(r'(\d+(?:\.\d+)?)\s*kg', txt)
        if m:
            data['weight'] = float(m.group(1))
            break

    # Exhibition time: find a cell that is exactly x.xx in the official
    # 6-second range. Restricting to cell-level values prevents picking 5R.
    for txt in texts:
        if re.fullmatch(r'\d\.\d{2}', txt):
            value = float(txt)
            if 5.0 <= value <= 8.0:
                data['exhibition_time'] = value
                break

    # Tilt: official page shows e.g. -0.5, 0.0, 3.0. Use a cell-level
    # numeric value after exhibition time when possible.
    exhibition_seen = False
    for txt in texts:
        if re.fullmatch(r'\d\.\d{2}', txt) and data['exhibition_time'] is not None:
            if abs(float(txt) - data['exhibition_time']) < 1e-9:
                exhibition_seen = True
                continue
        if exhibition_seen and re.fullmatch(r'-?\d+(?:\.\d+)?', txt):
            value = float(txt)
            if -3.0 <= value <= 3.0:
                data['tilt'] = value
                break

    if '新' in joined:
        data['propeller_changed'] = True

    for part in ('ピストンリング', 'ピストン', '電気', 'キャブ', 'シリンダ', 'シャフト', 'ギヤ', 'キャリボ'):
        if part in joined and part not in data['parts_exchange']:
            data['parts_exchange'].append(part)

    return data


def parse_beforeinfo(jcd: str, r_idx: int, today: str):
    """Parse official pre-race information for all six boats.

    The parser deliberately extracts by row/cell semantics instead of a broad
    regex over the whole page. If a field is unavailable, it remains None.
    """
    soup = BeautifulSoup(
        fetch_html('beforeinfo', {'rno': r_idx, 'jcd': jcd, 'hd': today}),
        'html.parser'
    )

    boats = {i: {
        'weight': None,
        'adjust_weight': None,
        'exhibition_time': None,
        'tilt': None,
        'propeller_changed': False,
        'parts_exchange': [],
        'start_exhibition': {'course': None, 'st': None},
        'original_exhibition': {
            'one_lap': None,
            'turn': None,
            'straight': None,
            'half_lap': None,
            'source': None,
        },
    } for i in range(1, 7)}

    parsed_boats = 0
    for row in soup.find_all('tr'):
        data = _extract_beforeinfo_row(row)
        if not data:
            continue
        boat_no = data.pop('boat_no')
        # Prefer the row with actual exhibition data when duplicate responsive
        # markup exists; otherwise keep the first valid row.
        if boats[boat_no]['exhibition_time'] is None or data['exhibition_time'] is not None:
            boats[boat_no].update(data)

    parsed_boats = sum(1 for i in range(1, 7) if boats[i]['exhibition_time'] is not None)

    # ----------------------------------------------------------------------
    # Start exhibition
    # ----------------------------------------------------------------------
    # The official page exposes "course / ST" as a separate table. We keep the
    # current boat-index association only when the HTML explicitly provides a
    # racer/boat identity. We do NOT infer boat=course when the page doesn't
    # identify the racer, because that would corrupt the historical dataset.
    start_rows = []
    marker = soup.find(string=re.compile('スタート展示'))
    if marker is not None:
        # Search forward from the marker's nearest table/container.
        container = marker.parent
        parent = container.parent if container and container.parent else soup
        for row in parent.find_all('tr'):
            texts = [clean_text(td.get_text(' ', strip=True)) for td in row.find_all('td')]
            if texts:
                start_rows.append(texts)

    # If no explicit identity is exposed, leave start_exhibition empty rather
    # than assigning ST to the wrong boat. A future parser can use image alt or
    # a stable racer identifier if the official markup exposes one.
    _ = start_rows

    weather = {
        'air_temp': None,
        'wind_speed': None,
        'wind_direction': None,
        'water_temp': None,
        'wave_height': None,
        'weather': None,
    }
    page_text = clean_text(soup.get_text(' ', strip=True))
    patterns = {
        'air_temp': r'気温\s*(-?\d+(?:\.\d+)?)',
        'wind_speed': r'風速\s*(-?\d+(?:\.\d+)?)m',
        'water_temp': r'水温\s*(-?\d+(?:\.\d+)?)',
        'wave_height': r'波高\s*(-?\d+(?:\.\d+)?)cm',
    }
    for key, pattern in patterns.items():
        m = re.search(pattern, page_text)
        if m:
            weather[key] = float(m.group(1))

    for candidate in ('晴', '曇り', '雨', '雪'):
        if candidate in page_text:
            weather['weather'] = candidate
            break

    print(f'  beforeinfo jcd={jcd} r={r_idx}: exhibition {parsed_boats}/6')

    return {
        'boats': list(boats.values()),
        'weather': weather,
        'source': 'boatrace_official_beforeinfo',
        'updated_at': datetime.now().isoformat(timespec='seconds'),
    }

# ============================================================================
# VALIDATION / DIAGNOSTICS
# ----------------------------------------------------------------------------
# This section never corrects data automatically. Its only job is to make a
# boat-to-racer mapping bug (or any other extraction gap) immediately visible
# in the GitHub Actions log, instead of silently shipping bad JSON. Compare
# against `_raw_cells`, which is the least-processed evidence we have for
# what the official page actually said for this specific <tr>.
# ============================================================================

_RAW_IDENTITY_RE = re.compile(
    r'^(?P<no>\d+)\s*/\s*(?P<cls>[AB][12])\s+(?P<name>.+?)\s+\S+/\S+\s+\d+歳'
)


def _parse_raw_identity(raw_cells):
    """Pull (racer_no, name) back out of the raw identity cell for cross-check.

    Expected shape of raw_cells[2], e.g.:
      "5243 / B1 三馬 崇史 広島/広島 26歳/53.5kg"
    This is validation-only; it must never be used as the primary data path.
    """
    if not raw_cells or len(raw_cells) < 3:
        return None, None
    m = _RAW_IDENTITY_RE.match(raw_cells[2])
    if not m:
        return None, None
    return int(m.group('no')), clean_text(m.group('name'))


def validate_race(jcd: str, race: dict):
    """Log [VALIDATION ERROR]/[WARNING] diagnostics for one race.

    Checks, per the audit requirements:
      - racers.length == 6, no == [1..6] with no duplicates
      - each boat's JSON racer_no/name matches its own _raw_cells identity
      - per-field extraction success counts (names/racer_no/national/local/
        motor/boat/exhibition), so a partial-extraction regression is visible
        even when it doesn't produce an outright placeholder value.
    """
    racers = race.get('racers', [])
    race_no = race.get('race_no')

    nos = [r.get('no') for r in racers]
    if len(racers) != 6 or sorted(n for n in nos if n is not None) != [1, 2, 3, 4, 5, 6]:
        print(f'[VALIDATION ERROR] jcd={jcd} Race {race_no}: 艇番が1〜6で揃っていません -> {nos}')

    counts = {
        'names': 0,
        'racer_no': 0,
        'national': 0,
        'local': 0,
        'motor': 0,
        'boat': 0,
        'exhibition': 0,
    }

    for r in racers:
        boat_no = r.get('no')
        raw_no, raw_name = _parse_raw_identity(r.get('_raw_cells'))

        if r.get('name') and not str(r['name']).startswith('選手'):
            counts['names'] += 1
        if r.get('racer_no') is not None:
            counts['racer_no'] += 1
        if (r.get('national') or {}).get('win_rate') is not None:
            counts['national'] += 1
        if (r.get('local') or {}).get('win_rate') is not None:
            counts['local'] += 1
        if (r.get('motor') or {}).get('no') is not None:
            counts['motor'] += 1
        if (r.get('boat') or {}).get('no') is not None:
            counts['boat'] += 1
        if (r.get('preview') or {}).get('exhibition_time') is not None:
            counts['exhibition'] += 1

        if raw_no is not None and r.get('racer_no') is not None and raw_no != r['racer_no']:
            print(
                f"[VALIDATION ERROR] jcd={jcd} Race {race_no} Boat {boat_no}: "
                f"JSON racer_no={r['racer_no']} Raw racer_no={raw_no}"
            )
        if raw_name and r.get('name') and raw_name not in r['name'] and r['name'] not in raw_name:
            print(
                f"[VALIDATION ERROR] jcd={jcd} Race {race_no} Boat {boat_no}: "
                f"JSON name='{r['name']}' Raw name='{raw_name}'"
            )

    print(
        f"[Race {race_no}] racers: {len(racers)}/6 "
        f"names: {counts['names']}/6 racer_no: {counts['racer_no']}/6 "
        f"national: {counts['national']}/6 local: {counts['local']}/6 "
        f"motor: {counts['motor']}/6 boat: {counts['boat']}/6 "
        f"exhibition: {counts['exhibition']}/6"
    )
    for field, ok in counts.items():
        if ok < 6:
            print(f'[WARNING] jcd={jcd} Race {race_no}: {field} が {ok}/6 艇分しか取得できていません')


def rank_smaller_is_better(values):
    valid = [(i, v) for i, v in enumerate(values) if isinstance(v, (int, float))]
    valid.sort(key=lambda x: x[1])
    rank = [None] * len(values)
    for pos, (i, _) in enumerate(valid, 1):
        rank[i] = pos
    return rank


def rank_larger_is_better(values):
    valid = [(i, v) for i, v in enumerate(values) if isinstance(v, (int, float))]
    valid.sort(key=lambda x: x[1], reverse=True)
    rank = [None] * len(values)
    for pos, (i, _) in enumerate(valid, 1):
        rank[i] = pos
    return rank


def merge_race_data(card, before):
    racers = card['racers']
    if before and before.get('boats'):
        for racer, preview in zip(racers, before['boats']):
            racer['preview'] = preview
    else:
        for racer in racers:
            racer['preview'] = {
                'weight': None,
                'adjust_weight': None,
                'exhibition_time': None,
                'tilt': None,
                'propeller_changed': False,
                'parts_exchange': [],
                'start_exhibition': {'course': None, 'st': None},
                'original_exhibition': {
                    'one_lap': None, 'turn': None, 'straight': None, 'half_lap': None, 'source': None
                },
            }

    # Weather is race-level, not racer-level. Only add the object when at least
    # one field was successfully observed; this follows the project's policy of
    # omitting unavailable optional data instead of filling it with nulls.
    if before and before.get('weather'):
        weather = before['weather']
        if any(v is not None for v in weather.values()):
            card['weather'] = weather

    # レース内順位はAPI/JSONから直接予想ロジックに使いやすいように保存。
    for field, getter, higher_better in [
        ('national_win_rate_rank', lambda r: r['national']['win_rate'], True),
        ('local_win_rate_rank', lambda r: r['local']['win_rate'], True),
        ('motor_top2_rate_rank', lambda r: r['motor']['top2_rate'], True),
        ('exhibition_time_rank', lambda r: r['preview']['exhibition_time'], False),
    ]:
        vals = [getter(r) for r in racers]
        ranks = rank_larger_is_better(vals) if higher_better else rank_smaller_is_better(vals)
        for i, r in enumerate(racers):
            r[field] = ranks[i]

    return card


def fetch_active_stadiums(today):
    try:
        soup = BeautifulSoup(fetch_html('index', {'hd': today}, timeout=12), 'html.parser')
        jcds = set()
        for a in soup.find_all('a', href=True):
            m = re.search(r'jcd=(\d{2})', a['href'])
            if m:
                jcds.add(m.group(1))
        if jcds:
            return sorted(jcds)
    except Exception as exc:
        print(f'開催場取得失敗: {exc}')
    return [f'{i:02d}' for i in range(1, 25)]


def fetch_one_race(jcd, r_idx, today, include_before=True, tide=None):
    card = parse_race_card(jcd, r_idx, today)
    before = None
    if include_before:
        try:
            before = parse_beforeinfo(jcd, r_idx, today)
        except Exception as exc:
            print(f'直前情報取得失敗 jcd={jcd} r={r_idx}: {exc}')
    race = merge_race_data(card, before)
    # Tide is optional. If unavailable, do not create an empty/null field.
    if tide:
        race['tide'] = tide
    return race


def process_stadium(jcd, today, include_before=True, tide_config=None):
    tide_config = tide_config or {}
    # IMPORTANT: fetch JMA tide ONCE per stadium/day, never once per race.
    daily_tide = get_daily_tide(jcd, today, tide_config)

    # 公式サイトへの負荷を抑えるため、現行よりやや控えめな並列数。
    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = [
            executor.submit(fetch_one_race, jcd, rno, today, include_before, daily_tide)
            for rno in range(1, 13)
        ]
        races = []
        for rno, future in enumerate(futures, 1):
            try:
                races.append(future.result())
            except Exception as exc:
                races.append({'race_no': rno, 'racers': [], 'error': str(exc)})

    races.sort(key=lambda x: x['race_no'])

    # Validate every race before writing it. This never mutates the data;
    # it only prints [VALIDATION ERROR]/[WARNING] so mapping regressions are
    # visible in the GitHub Actions log instead of shipping silently.
    for race in races:
        try:
            validate_race(jcd, race)
        except Exception as exc:
            print(f'[WARNING] jcd={jcd} Race {race.get("race_no")}: 検証処理自体が失敗しました: {exc}')

    payload = {
        'schema_version': 2,
        'date': today,
        'stadium_code': jcd,
        'races': races,
        'updated_at': datetime.now().isoformat(timespec='seconds'),
        'data_contract': {
            'stable': 'race_card',
            'realtime': 'before_info',
            'optional': 'original_exhibition',
        },
    }
    os.makedirs('data', exist_ok=True)
    path = f'data/{today}_{jcd}.json'
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    print(f'✅ {jcd}: {path}')


def main():
    today = datetime.now().strftime('%Y%m%d')
    active = fetch_active_stadiums(today)
    tide_config = load_tide_station_config()
    print(f'[{today}] enhanced fetch: {active}')
    os.makedirs('data', exist_ok=True)

    # include_before=True の実行は「直前情報更新モード」。
    # 0時の初回取得では False にしてもよい。
    include_before = os.getenv('INCLUDE_BEFORE_INFO', '1') == '1'

    with ThreadPoolExecutor(max_workers=3) as executor:
        futures = [executor.submit(process_stadium, jcd, today, include_before, tide_config) for jcd in active]
        for future in futures:
            future.result()

    # 非開催場は既存UIが読める形を維持。
    for jcd in [f'{i:02d}' for i in range(1, 25)]:
        path = f'data/{today}_{jcd}.json'
        if os.path.exists(path):
            continue
        payload = {
            'schema_version': 2,
            'date': today,
            'stadium_code': jcd,
            'races': [
                {'race_no': rno, 'racers': []} for rno in range(1, 13)
            ],
            'updated_at': datetime.now().isoformat(timespec='seconds'),
            'data_contract': {
                'stable': 'race_card',
                'realtime': 'before_info',
                'optional': 'original_exhibition',
            },
        }
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)


# DATA SOURCES
# - BOAT RACE official site: race card / before-info
# - JMA official tide table: astronomical tide predictions
# - Venue-to-JMA mapping: config/tide_stations.json
# Keep source URLs and field semantics in comments/config so another AI can
# safely audit or replace an extraction rule without guessing its purpose.
if __name__ == '__main__':
    main()
