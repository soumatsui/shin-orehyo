import json
import os
from datetime import datetime
import urllib.request
import urllib.parse
from bs4 import BeautifulSoup

def fetch_daily_data():
    # 本日の日付を取得 (YYYYMMDD形式)
    today = datetime.now().strftime('%Y%m%d')
    print(f"[{today}] ボートレースデータの自動取得を開始します...")

    # 保存先ディレクトリの作成
    os.makedirs('data', exist_ok=True)

    # 全24場の会場コード (01〜24)
    stadiums = [f"{i:02d}" for i in range(1, 25)]

    for jcd in stadiums:
        # ボートレース公式の出走表URL例
        url = f"https://www.boatrace.jp/owpc/pc/race/index?jcd={jcd}&hd={today}"
        
        try:
            req = urllib.request.Request(
                url, 
                headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}
            )
            with urllib.request.urlopen(req) as response:
                html = response.read().decode('utf-8')
                
            # BeautifulSoupで解析
            soup = BeautifulSoup(html, 'html.parser')
            
            # ここで各レースの出走表や選手データを抽出し、構造化データを作成します
            # ※公式サイトの構造に合わせたパース処理をここに記述します
            
            race_data = {
                "date": today,
                "stadium_code": jcd,
                "status": "success",
                "message": "データを正常に取得しました"
            }

            # JSONとして保存
            file_path = f"data/{today}_{jcd}.json"
            with open(file_path, 'w', encoding='utf-8') as f:
                json.dump(race_data, f, ensure_ascii=False, indent=4)
                
            print(f"場コード {jcd} のデータを保存しました。")
            
        except Exception as e:
            print(f"場コード {jcd} の取得に失敗しました: {e}")

if __name__ == "__main__":
    fetch_daily_data()