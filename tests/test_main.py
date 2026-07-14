import unittest

from main import extract_uid, normalize_fans_url


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




if __name__ == "__main__":
    unittest.main()
