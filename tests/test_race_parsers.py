import importlib.util
from pathlib import Path
from unittest.mock import patch
import unittest

from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('scraper', ROOT / 'fetch_boat_data_enhanced_with_tide.py')
scraper = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(scraper)


class RaceParserTest(unittest.TestCase):
    def test_start_exhibition_uses_image_boat_number_not_row_order(self):
        html = (ROOT / 'tests/fixtures/beforeinfo_start_exhibition.html').read_text()
        starts, _ = scraper._parse_start_exhibition(BeautifulSoup(html, 'html.parser'))
        self.assertEqual(starts[3], {'course': 2, 'st': 0.08, 'flying': False})
        self.assertEqual(starts[2], {'course': 3, 'st': 0.01, 'flying': True})

    def test_result_finish_order_and_payouts(self):
        html = (ROOT / 'tests/fixtures/raceresult.html').read_text()
        with patch.object(scraper, 'fetch_html', return_value=html):
            result = scraper.parse_race_result('07', 1, '20260917')
        self.assertEqual(result['finish_order'], [4, 1, 6, 2, 5, 3])
        self.assertEqual(result['winning_technique'], 'まくり')
        self.assertEqual(result['payouts']['3連単'], {'combination': '4-1-6', 'amount': 1230})
        self.assertEqual(result['payouts']['3連複'], {'combination': '4=1=6', 'amount': 1100})
        self.assertEqual(result['payouts']['2連単'], {'combination': '4-1', 'amount': 980})
        self.assertEqual(result['payouts']['2連複'], {'combination': '1=4', 'amount': 870})
        self.assertEqual(result['payouts']['拡連複'], [
            {'combination': '1=4', 'amount': 1020},
            {'combination': '4=6', 'amount': 2340},
        ])
        self.assertEqual(result['payouts']['単勝'], {'combination': '4', 'amount': 130})
        self.assertEqual(result['payouts']['複勝'], [
            {'combination': '4', 'amount': 100},
            {'combination': '1', 'amount': 1230},
        ])

    def test_payout_amount_accepts_official_currency_variants(self):
        self.assertEqual(scraper.parse_payout_amount('1230円'), 1230)
        self.assertEqual(scraper.parse_payout_amount('1,230円'), 1230)
        self.assertEqual(scraper.parse_payout_amount('¥1,230'), 1230)
        self.assertEqual(scraper.parse_payout_amount('￥１，２３０円'), 1230)
        self.assertIsNone(scraper.parse_payout_amount('返還'))


if __name__ == '__main__':
    unittest.main()
