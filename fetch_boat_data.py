import json
import os
import re
from datetime import datetime
import urllib.request
from bs4 import BeautifulSoup
from concurrent.futures import ThreadPoolExecutor

USER_AGENT = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'

def get_active_stadiums(today):
    """本日開催されているレース場のJCDコード(01〜24)のみを自動取得"""
    url = f"https://www.boatrace.jp/owpc/pc/race/index?hd={today}"
    req = urllib.request.Request(url, headers={'User-Agent': USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=10) as response:
            html = response.read().decode('utf-8')
        soup = BeautifulSoup(html, 'html.parser')

        jcd_list = set()
        for a in soup.find_all('a', href=True):
            match = re.search(r'jcd=(\d{2})', a['href'])
            if match:
                jcd_list.add(match.group(1))

        if jcd_list:
            sorted_jcds = sorted(list(jcd_list))
            print(f"本日開催の場 ({len(sorted_jcds)}場): {sorted_jcds}")
            return sorted_jcds
    except Exception as e:
        print(f"開催場の取得に失敗したため、全場を対象にします: {e}")

    return [f"{i:02d}" for i in range(1, 25)]

def fetch_race_data(jcd, r_idx, today):
    """1レース分の出走表を取得"""
    url = f"https://www.boatrace.jp/owpc/pc/race/racelist?rno={r_idx}&jcd={jcd}&hd={today}"
    racers = []
    try:
        req = urllib.request.Request(url, headers={
            'User-Agent': USER_AGENT,
            'Accept-Language': 'ja,en-US;q=0.9,en;q=0.8',
            'Referer': 'https://www.boatrace.jp/owpc/pc/race/index',
        })
        with urllib.request.urlopen(req, timeout=10) as response:
            status = response.status
            html = response.read().decode('utf-8')

        # ページは取得できたが中身が想定と違う（ブロックページ等）場合の検知用
        if 'racersearch' not in html:
            print(f"⚠️ jcd={jcd} rno={r_idx}: status={status} だが選手データらしき内容が見当たりません（HTML長={len(html)}）")

        soup = BeautifulSoup(html, 'html.parser')
        name_links = soup.select('a[href*="racersearch/profile?toban="]')

        if len(name_links) < 6:
            print(f"⚠️ jcd={jcd} rno={r_idx}: 選手リンクが{len(name_links)}件しか見つかりません")

        for boat_no in range(1, 7):
            racer_name = f"選手{boat_no}号艇"
            racer_class = "A1"
            try:
                if len(name_links) >= boat_no:
                    link = name_links[boat_no - 1]
                    text = link.get_text(strip=True)
                    if text:
                        racer_name = text

                    row = link.find_parent('tr')
                    if row:
                        text_all = row.get_text()
                        for cls in ['A1', 'A2', 'B1', 'B2']:
                            if cls in text_all:
                                racer_class = cls
                                break
            except Exception as e:
                print(f"⚠️ jcd={jcd} rno={r_idx} boat={boat_no}: パース中に例外: {e}")

            racers.append({
                "no": boat_no,
                "name": racer_name,
                "class": racer_class
            })
    except Exception as e:
        print(f"❌ jcd={jcd} rno={r_idx}: リクエスト自体が失敗しました: {type(e).__name__}: {e}")
        for boat_no in range(1, 7):
            racers.append({
                "no": boat_no,
                "name": f"選手{boat_no}号艇",
                "class": "A1"
            })

    return {
        "race_no": r_idx,
        "racers": racers
    }

def process_stadium(jcd, today):
    """1場分(全12レース)を並列取得"""
    with ThreadPoolExecutor(max_workers=6) as executor:
        futures = [executor.submit(fetch_race_data, jcd, r_idx, today) for r_idx in range(1, 13)]
        races_data = [f.result() for f in futures]

    races_data.sort(key=lambda x: x["race_no"])

    daily_stadium_data = {
        "date": today,
        "stadium_code": jcd,
        "races": races_data,
        "updated_at": datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    }

    file_path = f"data/{today}_{jcd}.json"
    with open(file_path, 'w', encoding='utf-8') as f:
        json.dump(daily_stadium_data, f, ensure_ascii=False, indent=4)

    print(f"✅ 場コード {jcd} の処理が完了")

def fetch_daily_data():
    today = datetime.now().strftime('%Y%m%d')
    print(f"[{today}] 高速データ自動取得を開始します...")
    os.makedirs('data', exist_ok=True)

    active_stadiums = get_active_stadiums(today)

    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = [executor.submit(process_stadium, jcd, today) for jcd in active_stadiums]
        for future in futures:
            future.result()

    all_stadiums = [f"{i:02d}" for i in range(1, 25)]
    for jcd in all_stadiums:
        file_path = f"data/{today}_{jcd}.json"
        if not os.path.exists(file_path):
            dummy_races = [{
                "race_no": r_idx,
                "racers": [{"no": b, "name": f"選手{b}号艇 (非開催)", "class": "A1"} for b in range(1, 7)]
            } for r_idx in range(1, 13)]

            daily_stadium_data = {
                "date": today,
                "stadium_code": jcd,
                "races": dummy_races,
                "updated_at": datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            }
            with open(file_path, 'w', encoding='utf-8') as f:
                json.dump(daily_stadium_data, f, ensure_ascii=False, indent=4)

    print("🎉 全データの高速取得・生成が完了しました！")

if __name__ == "__main__":
    fetch_daily_data()
