import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from unittest.mock import patch

from main import (
    Candidate,
    drop_candidate,
    extract_uid,
    load_candidates,
    normalize_fans_url,
    validate_args,
    write_candidates,
)


class NormalizeFansUrlTests(unittest.TestCase):
    def test_adds_fans_relation(self):
        self.assertEqual(
            normalize_fans_url("https://weibo.com/u/page/follow/2802216802"),
            "https://weibo.com/u/page/follow/2802216802?relate=fans",
        )

    def test_preserves_other_query_parameters(self):
        result = normalize_fans_url(
            "https://weibo.com/u/page/follow/2802216802?foo=bar&relate=follow"
        )
        self.assertIn("foo=bar", result)
        self.assertIn("relate=fans", result)

    def test_rejects_unrelated_url(self):
        with self.assertRaises(ValueError):
            normalize_fans_url("https://example.com/")


class ExtractUidTests(unittest.TestCase):
    def test_extracts_uid_from_profile_link(self):
        self.assertEqual(
            extract_uid("某用户", ["https://weibo.com/u/4009663657"]),
            "4009663657",
        )

    def test_falls_back_to_generated_name(self):
        self.assertEqual(extract_uid("用户4009663657\n暂无简介", []), "4009663657")


class ValidateArgsTests(unittest.TestCase):
    def test_clean_allows_missing_limit(self):
        validate_args(
            Namespace(
                command="clean",
                confirm=True,
                limit=None,
                min_delay=2.0,
                max_delay=6.0,
                max_scrolls=500,
            )
        )

    def test_clean_rejects_non_positive_limit(self):
        with self.assertRaises(ValueError):
            validate_args(
                Namespace(
                    command="clean",
                    confirm=True,
                    limit=0,
                    min_delay=2.0,
                    max_delay=6.0,
                    max_scrolls=500,
                )
            )


class CandidateListTests(unittest.TestCase):
    def test_drop_candidate_updates_memory_and_disk(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            json_path = data_dir / "candidates.json"
            csv_path = data_dir / "candidates.csv"
            pending = [
                Candidate("1", "a", "兴趣推荐", "未回关", ""),
                Candidate("2", "b", "兴趣推荐", "未回关", ""),
            ]
            with patch.multiple(
                "main",
                DATA_DIR=data_dir,
                CANDIDATES_JSON=json_path,
                CANDIDATES_CSV=csv_path,
            ):
                write_candidates(pending)
                drop_candidate(pending, "1")
                self.assertEqual([item.uid for item in pending], ["2"])
                self.assertEqual([item.uid for item in load_candidates()], ["2"])


if __name__ == "__main__":
    unittest.main()
