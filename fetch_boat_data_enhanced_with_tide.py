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


def find_data_rows(soup: BeautifulSoup):
    # 公式出走表の現行構造を優先し、少し緩めのフォールバックも用意。
    rows = soup.select('tbody.is-fs12 tr')
    if not rows:
        rows = soup.select('tr.is-fs12')
    return rows


def parse_race_card(jcd: str, r_idx: int, today: str):
    soup = BeautifulSoup(
        fetch_html('racelist', {'rno': r_idx, 'jcd': jcd, 'hd': today}),
        'html.parser'
    )

    racers = []
    name_links = soup.select('a[href*="racersearch/profile?toban="]')

    rows = []
    for row in find_data_rows(soup):
        text = clean_text(row.get_text(' ', strip=True))
        if any(c in text for c in ('A1', 'A2', 'B1', 'B2')) and re.search(r'\b[1-6]\b', text):
            rows.append(row)

    # まず行単位で構造化し、無理なら既存実装に近い名前抽出へフォールバック。
    parsed_by_boat = {}
    for row in rows:
        cells = row.find_all('td')
        if not cells:
            continue
        text = clean_text(row.get_text(' ', strip=True))
        boat_match = re.match(r'^([1-6])\b', clean_text(cells[0].get_text(' ', strip=True)))
        if not boat_match:
            boat_match = re.search(r'\b([1-6])\b', text)
        if not boat_match:
            continue
        boat_no = int(boat_match.group(1))

        link = row.select_one('a[href*="racersearch/profile?toban="]')
        name = clean_text(link.get_text(' ', strip=True)) if link else ''
        racer_class = next((c for c in ('A1', 'A2', 'B1', 'B2') if c in text), None)

        # セル位置は公式ページ構造に依存するため、数値の塊も保持しておく。
        cell_texts = [clean_text(c.get_text(' ', strip=True)) for c in cells]
        numbers = [safe_float(c) for c in cell_texts]

        item = {
            'no': boat_no,
            'name': name or f'選手{boat_no}号艇',
            'class': racer_class or 'A1',
            'racer_no': None,
            'weight': None,
            'national': {'win_rate': None, 'top2_rate': None, 'top3_rate': None},
            'local': {'win_rate': None, 'top2_rate': None, 'top3_rate': None},
            'motor': {'no': None, 'top2_rate': None, 'top3_rate': None},
            'boat': {'no': None, 'top2_rate': None, 'top3_rate': None},
            'avg_st': None,
            'f_count': None,
            'l_count': None,
            '_raw_cells': cell_texts,
            '_raw_numbers': numbers,
        }

        # 公式出走表の表示順は F/L -> 平均ST -> 全国3率 -> 当地3率
        # -> モーター -> ボート、という並びなので、F/Lを起点に
        # 構造に依存しすぎない形で数値を拾う。
        stat_match = re.search(
            r'F(?P<f>\d+)\s*L(?P<l>\d+)\s*'
            r'(?P<avg>\d+\.\d+)\s+'
            r'(?P<nw>\d+\.\d+)\s+(?P<n2>\d+\.\d+)\s+(?P<n3>\d+\.\d+)\s+'
            r'(?P<lw>\d+\.\d+)\s+(?P<l2>\d+\.\d+)\s+(?P<l3>\d+\.\d+)\s+'
            r'(?P<mno>\d+)\s+(?P<m2>\d+\.\d+)\s+(?P<m3>\d+\.\d+)\s+'
            r'(?P<bno>\d+)\s+(?P<b2>\d+\.\d+)\s+(?P<b3>\d+\.\d+)',
            text
        )
        if stat_match:
            g = stat_match.groupdict()
            item['f_count'] = int(g['f'])
            item['l_count'] = int(g['l'])
            item['avg_st'] = float(g['avg'])
            item['national'] = {
                'win_rate': float(g['nw']), 'top2_rate': float(g['n2']), 'top3_rate': float(g['n3'])
            }
            item['local'] = {
                'win_rate': float(g['lw']), 'top2_rate': float(g['l2']), 'top3_rate': float(g['l3'])
            }
            item['motor'] = {
                'no': int(g['mno']), 'top2_rate': float(g['m2']), 'top3_rate': float(g['m3'])
            }
            item['boat'] = {
                'no': int(g['bno']), 'top2_rate': float(g['b2']), 'top3_rate': float(g['b3'])
            }

        # 公式の選手リンク親行から登録番号/級別を補足。
        if link:
            m = re.search(r'toban=(\d+)', link.get('href', ''))
            if m:
                item['racer_no'] = int(m.group(1))

        # できるだけラベル/見出しを利用して抽出。取得できない値はNone。
        # 公式のセル並びが変わっても、最低限の名前・級別は失わない。
        for idx, txt in enumerate(cell_texts):
            if item['avg_st'] is None and re.fullmatch(r'0\.\d{2}', txt):
                item['avg_st'] = safe_float(txt)
            if item['f_count'] is None:
                mf = re.search(r'F(\d+)', txt)
                if mf:
                    item['f_count'] = int(mf.group(1))
            if item['l_count'] is None:
                ml = re.search(r'L(\d+)', txt)
                if ml:
                    item['l_count'] = int(ml.group(1))

        # 見出しベースの数値列を探索。
        headers = []
        for th in soup.select('thead th, tr.is-fs11 th, tr.is-fs12 th'):
            headers.append(clean_text(th.get_text(' ', strip=True)))
        header_text = ' '.join(headers)
        _ = header_text  # 将来のセレクタ調整用

        parsed_by_boat[boat_no] = item

    # 名前リンクを使った従来方式で6艇を必ず埋める。
    for boat_no in range(1, 7):
        item = parsed_by_boat.get(boat_no)
        if item is None:
            racer_name = f'選手{boat_no}号艇'
            racer_class = 'A1'
            if len(name_links) >= boat_no:
                link = name_links[boat_no - 1]
                racer_name = clean_text(link.get_text(' ', strip=True)) or racer_name
                row = link.find_parent('tr')
                if row:
                    text = clean_text(row.get_text(' ', strip=True))
                    racer_class = next((c for c in ('A1', 'A2', 'B1', 'B2') if c in text), racer_class)
            item = {
                'no': boat_no,
                'name': racer_name,
                'class': racer_class,
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
        racers.append(item)

    return {
        'race_no': r_idx,
        'racers': racers,
    }


def parse_beforeinfo(jcd: str, r_idx: int, today: str):
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

    # 公式直前情報の艇別テーブル。is-fs12 行を広く走査して数値列を取り込む。
    for row in soup.select('tr'):
        cells = [clean_text(td.get_text(' ', strip=True)) for td in row.find_all('td')]
        if not cells:
            continue
        joined = ' '.join(cells)
        boat_match = re.search(r'^([1-6])\b', joined)
        if not boat_match:
            continue
        boat_no = int(boat_match.group(1))
        if boat_no not in boats:
            continue

        # 表示上の列: 体重 / 展示タイム / チルト / プロペラ / 部品交換 など。
        nums = re.findall(r'-?\d+(?:\.\d+)?', joined)
        # 体重(kg) と展示タイム(6.xx) とチルト(-0.5等)を識別。
        weight = next((float(x) for x in nums if 35 <= float(x) <= 80), None)
        exhibit = next((float(x) for x in nums if 5.0 <= float(x) <= 8.0), None)
        tilt = next((float(x) for x in nums if -3.0 <= float(x) <= 3.0 and abs(float(x) - (weight or 999)) > 0.01), None)
        if weight is not None:
            boats[boat_no]['weight'] = weight
        if exhibit is not None:
            boats[boat_no]['exhibition_time'] = exhibit
        if tilt is not None:
            boats[boat_no]['tilt'] = tilt

        lower = joined.lower()
        if '新' in joined or 'プロペラ' in joined:
            boats[boat_no]['propeller_changed'] = '新' in joined
        for part in ('ピストン', 'ピストンリング', '電気', 'キャブ', 'シリンダ', 'シャフト', 'ギヤ', 'キャリボ'):
            if part in joined and part not in boats[boat_no]['parts_exchange']:
                boats[boat_no]['parts_exchange'].append(part)

    # スタート展示: 1艇ごとのコース/STを抽出。
    start_section = soup.find(string=re.compile('スタート展示'))
    if start_section is not None:
        parent = start_section.parent
        scope = parent.parent if parent and parent.parent else soup
        for row in scope.select('tr'):
            cells = [clean_text(td.get_text(' ', strip=True)) for td in row.find_all('td')]
            if len(cells) < 2:
                continue
            joined = ' '.join(cells)
            nums = re.findall(r'-?(?:\d+\.\d+|\d+)', joined)
            # 進入は1〜6、STは .xx / F.xx / L.xx など。
            course = next((int(x) for x in nums if 1 <= int(float(x)) <= 6), None)
            st_match = re.search(r'([FL])?\s*\.?(\d{1,2})', joined)
            if course is None or not st_match:
                continue
            st = float(f'0.{st_match.group(2).zfill(2)}')
            if st_match.group(1) == 'F':
                st = -st
            if len(boats) >= course:
                boats[course]['start_exhibition']['course'] = course
                boats[course]['start_exhibition']['st'] = st

    # 水面気象情報はページ全体のラベル付近から取得。
    weather = {
        'air_temp': None,
        'wind_speed': None,
        'wind_direction': None,
        'water_temp': None,
        'wave_height': None,
        'weather': None,
    }
    page_text = clean_text(soup.get_text(' ', strip=True))
    for key, pattern in {
        'air_temp': r'気温\s*(-?\d+(?:\.\d+)?)',
        'wind_speed': r'風速\s*(-?\d+(?:\.\d+)?)',
        'water_temp': r'水温\s*(-?\d+(?:\.\d+)?)',
        'wave_height': r'波高\s*(-?\d+(?:\.\d+)?)',
    }.items():
        m = re.search(pattern, page_text)
        if m:
            weather[key] = float(m.group(1))

    return {
        'boats': list(boats.values()),
        'weather': weather,
        'source': 'boatrace_official_beforeinfo',
        'updated_at': datetime.now().isoformat(timespec='seconds'),
    }


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


def build_prediction_features(racers):
    features = {}
    fields_larger = [
        ('national_win_rate', lambda r: r['national']['win_rate']),
        ('local_win_rate', lambda r: r['local']['win_rate']),
        ('motor_top2_rate', lambda r: r['motor']['top2_rate']),
        ('boat_top2_rate', lambda r: r['boat']['top2_rate']),
    ]
    for field_name, getter in fields_larger:
        values = [getter(r) for r in racers]
        ranks = rank_larger_is_better(values)
        for i, r in enumerate(racers):
            features.setdefault(str(r['no']), {})[field_name + '_rank'] = ranks[i]

    for source, key in [('before', 'exhibition_time')]:
        values = [source for _ in racers]
        _ = values
        ranks = rank_smaller_is_better([None] * len(racers))
        _ = (key, ranks)

    return features


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
