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
        self.assertEqual(starts[3], {'course': 2, 'st': 0.08})
        self.assertEqual(starts[2], {'course': 3, 'st': 0.01})

    def test_result_finish_order_and_payouts(self):
        html = (ROOT / 'tests/fixtures/raceresult.html').read_text()
        with patch.object(scraper, 'fetch_html', return_value=html):
            result = scraper.parse_race_result('07', 1, '20260917')
        self.assertEqual(result['finish_order'], [4, 1, 6, 2, 5, 3])
        self.assertEqual(result['winning_technique'], 'まくり')
        self.assertEqual(result['payouts']['3連単'], {'combination': '4-1-6', 'amount': 1230})


if __name__ == '__main__':
    unittest.main()
