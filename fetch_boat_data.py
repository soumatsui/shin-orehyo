import json
import os
from datetime import datetime
import urllib.request
from bs4 import BeautifulSoup

def fetch_daily_data():
    today = datetime.now().strftime('%Y%m%d')
    print(f"[{today}] ボートレースデータの自動取得を開始します...")

    # 保存先ディレクトリの作成
    os.makedirs('data', exist_ok=True)

    # 全24場の会場コード (01〜24)
    stadiums = [f"{i:02d}" for i in range(1, 25)]

    for jcd in stadiums:
        races_data = []
        
        for r_idx in range(1, 13):
            url = f"https://www.boatrace.jp/owpc/pc/race/racelist?rno={r_idx}&jcd={jcd}&hd={today}"
            
            racers = []
            try:
                req = urllib.request.Request(
                    url, 
                    headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
                )
                with urllib.request.urlopen(req, timeout=10) as response:
                    html = response.read().decode('utf-8')
                
                soup = BeautifulSoup(html, 'html.parser')
                
                # 各艇（1〜6号艇）の情報を取得
                for boat_no in range(1, 7):
                    racer_name = f"選手{boat_no}号艇"
                    racer_class = "A1"
                    
                    try:
                        row = soup.select('tr.is-fs12')[boat_no - 1] if len(soup.select('tr.is-fs12')) >= 6 else None
                        if row:
                            name_tag = row.select_one('a[href*="racerbin"]')
                            if name_tag:
                                racer_name = name_tag.text.strip()
                            
                            text_all = row.text
                            for cls in ['A1', 'A2', 'B1', 'B2']:
                                if cls in text_all:
                                    racer_class = cls
                                    break
                    except Exception:
                        pass

                    racers.append({
                        "no": boat_no,
                        "name": racer_name if racer_name else f"選手{boat_no}号艇",
                        "class": racer_class
                    })
            
            except Exception as e:
                for boat_no in range(1, 7):
                    racers.append({
                        "no": boat_no,
                        "name": f"選手{boat_no}号艇 (取得エラー)",
                        "class": "A1"
                    })

            races_data.append({
                "race_no": r_idx,
                "racers": racers
            })

        daily_stadium_data = {
            "date": today,
            "stadium_code": jcd,
            "races": races_data,
            "updated_at": datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        }

        file_path = f"data/{today}_{jcd}.json"
        with open(file_path, 'w', encoding='utf-8') as f:
            json.dump(daily_stadium_data, f, ensure_ascii=False, indent=4)
        
        print(f"場コード {jcd}: 処理完了")

    print("すべての場のデータ処理が完了しました。")

if __name__ == "__main__":
    fetch_daily_data()
