"""PubMed 수집기(app/collectors/pubmed.py) 테스트. httpx·save_papers를 가짜로 바꿔서 네트워크·DB 없이 돈다.

실행 (backend/ 에서):  python -m unittest tests.test_pubmed
"""
import unittest
from datetime import date
from unittest import mock

from app.collectors import pubmed
from app.schemas import SavePapersResult

REAL_FETCH_BETWEEN = pubmed.fetch_papers_between  # CollectRangeTest가 가짜로 바꾸기 전의 원본


def esearch_response(pmids, count=None):
    resp = mock.Mock()
    resp.json.return_value = {"esearchresult": {"idlist": pmids, "count": str(len(pmids) if count is None else count)}}
    return resp


class MonthRangesTest(unittest.TestCase):
    def test_splits_by_calendar_month_and_clips_ends(self):
        self.assertEqual(pubmed.month_ranges(date(2024, 1, 15), date(2024, 3, 10)), [
            (date(2024, 1, 15), date(2024, 1, 31)),
            (date(2024, 2, 1), date(2024, 2, 29)),  # 윤년
            (date(2024, 3, 1), date(2024, 3, 10)),
        ])

    def test_two_years_is_24_months_across_year_end(self):
        ranges = pubmed.month_ranges(date(2024, 1, 1), date(2025, 12, 31))
        self.assertEqual(len(ranges), 24)
        self.assertEqual(ranges[11], (date(2024, 12, 1), date(2024, 12, 31)))
        self.assertEqual(ranges[12], (date(2025, 1, 1), date(2025, 1, 31)))

    def test_single_day_and_bad_order(self):
        self.assertEqual(pubmed.month_ranges(date(2025, 5, 5), date(2025, 5, 5)), [(date(2025, 5, 5), date(2025, 5, 5))])
        with self.assertRaises(ValueError):
            pubmed.month_ranges(date(2025, 2, 1), date(2025, 1, 31))


class FetchPmidsTest(unittest.TestCase):
    def setUp(self):
        self.get = self.enterContext(mock.patch.object(pubmed.httpx, "get", return_value=esearch_response(["1", "2"])))
        self.enterContext(mock.patch.object(pubmed, "_throttle"))

    def params(self):
        return self.get.call_args.kwargs["params"]

    def test_date_range_uses_mindate_maxdate(self):
        self.assertEqual(pubmed.fetch_pmids(retmax=50, date_range=(date(2024, 1, 1), date(2024, 1, 31))), ["1", "2"])
        p = self.params()
        self.assertEqual((p["datetype"], p["mindate"], p["maxdate"]), ("pdat", "2024/01/01", "2024/01/31"))
        self.assertNotIn("reldate", p)

    def test_days_back_unchanged(self):
        pubmed.fetch_pmids(retmax=100, days_back=7)
        p = self.params()
        self.assertEqual((p["datetype"], p["reldate"], p["retmax"]), ("pdat", "7", "100"))
        self.assertNotIn("mindate", p)

    def test_query_excludes_animal_only_studies(self):
        pubmed.fetch_pmids(days_back=7)
        self.assertTrue(self.params()["term"].endswith('NOT ("Animals"[MeSH] NOT "Humans"[MeSH])'))

    def test_warns_when_truncated(self):
        self.get.return_value = esearch_response(["1", "2"], count=5)
        with self.assertLogs(pubmed.logger, "WARNING"):
            pubmed.fetch_pmids(retmax=2, days_back=7)


class CollectRangeTest(unittest.TestCase):
    def setUp(self):
        self.fetch = self.enterContext(mock.patch.object(
            pubmed, "fetch_papers_between", side_effect=lambda s, e: [f"{s:%m}-a", f"{s:%m}-b"]))
        self.save = self.enterContext(mock.patch.object(
            pubmed, "save_papers", side_effect=lambda papers: SavePapersResult(total=len(papers), inserted=1, skipped=len(papers) - 1)))

    def test_fetches_and_saves_month_by_month(self):
        result = pubmed.collect_range_and_store(date(2024, 11, 20), date(2025, 1, 5))
        self.assertEqual([c.args for c in self.fetch.call_args_list], [
            (date(2024, 11, 20), date(2024, 11, 30)), (date(2024, 12, 1), date(2024, 12, 31)),
            (date(2025, 1, 1), date(2025, 1, 5))])
        self.assertEqual([c.args[0] for c in self.save.call_args_list], [["11-a", "11-b"], ["12-a", "12-b"], ["01-a", "01-b"]])
        self.assertEqual((result.total, result.inserted, result.skipped), (6, 3, 3))

    def test_dry_run_does_not_save(self):
        result = pubmed.collect_range_and_store(date(2024, 1, 1), date(2024, 2, 29), dry_run=True)
        self.save.assert_not_called()
        self.assertEqual((result.total, result.inserted), (4, 0))

    def test_fetch_papers_between_asks_whole_range_up_to_esearch_max(self):
        with mock.patch.object(pubmed, "fetch_pmids", return_value=["9"]) as fp, \
                mock.patch.object(pubmed, "_fetch_and_parse", return_value=["paper"]) as parse:
            self.assertEqual(REAL_FETCH_BETWEEN(date(2024, 2, 1), date(2024, 2, 29)), ["paper"])
        self.assertEqual(fp.call_args.kwargs,
                         {"retmax": pubmed.ESEARCH_MAX, "date_range": (date(2024, 2, 1), date(2024, 2, 29))})
        parse.assert_called_once_with(["9"])


if __name__ == "__main__":
    unittest.main()
