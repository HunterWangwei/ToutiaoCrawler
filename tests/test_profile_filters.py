from datetime import datetime
from pathlib import Path
import tempfile
import unittest

from profile_ui import extract_profile_urls, profile_filter_reason
from toutiao_profile_crawler.crawler import clean_post_content, save_post


class ProfileFilterTests(unittest.TestCase):
    def setUp(self):
        self.start = datetime(2026, 8, 11, 0, 0, 0)
        self.end = datetime(2026, 8, 11, 23, 59, 59, 999999)

    def post(self, published: datetime | None, comments: int = 20) -> dict:
        return {
            "publish_timestamp": int(published.timestamp()) if published else 0,
            "comment_count": comments,
        }

    def test_low_comment_count_is_filtered(self):
        result = profile_filter_reason(self.post(self.start, 3), 10, None, None)
        self.assertEqual(result, ("low_comments", "评论数不足：3＜10"))

    def test_today_is_allowed(self):
        self.assertIsNone(
            profile_filter_reason(self.post(datetime(2026, 8, 11, 12, 30)), 10, self.start, self.end)
        )

    def test_yesterday_is_filtered(self):
        result = profile_filter_reason(
            self.post(datetime(2026, 8, 10, 12, 30)), 10, self.start, self.end
        )
        self.assertEqual(result[0], "time_filtered")
        self.assertIn("2026-08-10 12:30:00", result[1])

    def test_custom_range_includes_boundaries(self):
        self.assertIsNone(profile_filter_reason(self.post(self.start), 0, self.start, self.end))
        self.assertIsNone(profile_filter_reason(self.post(self.end), 0, self.start, self.end))

    def test_unknown_publish_time_is_filtered_when_time_limited(self):
        self.assertEqual(
            profile_filter_reason(self.post(None), 0, self.start, self.end),
            ("time_filtered", "发布时间未知"),
        )


class ProfileUrlImportTests(unittest.TestCase):
    def test_extracts_one_profile_per_line_and_removes_duplicates(self):
        first = "https://www.toutiao.com/c/user/token/abc123/?tab=wtt"
        second = "https://www.toutiao.com/c/user/token/xyz789/"
        text = f"{first}\n无效内容\n{second}\n{first}\n"
        self.assertEqual(extract_profile_urls(text), [first, second])

    def test_ignores_article_urls(self):
        self.assertEqual(extract_profile_urls("https://www.toutiao.com/article/123"), [])


class ProfileContentCleaningTests(unittest.TestCase):
    def test_removes_known_full_width_trailing_marks(self):
        self.assertEqual(clean_post_content("正文内容。 【gmj】"), "正文内容。")
        self.assertEqual(clean_post_content("正文内容。\n【lm】"), "正文内容。")

    def test_accepts_half_width_brackets_and_case(self):
        self.assertEqual(clean_post_content("正文内容。[LM]"), "正文内容。")

    def test_keeps_mark_inside_normal_content(self):
        self.assertEqual(clean_post_content("正文提到【lm】但后面还有文字"), "正文提到【lm】但后面还有文字")

    def test_saved_comments_do_not_include_like_count(self):
        class FakeCrawler:
            @staticmethod
            def download_images(post, folder: Path):
                folder.mkdir(parents=True, exist_ok=True)
                return []

        post = {"id": "123", "content": "正文", "images": []}
        comments = [{"text": "这是一条评论", "digg_count": 8}]
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            save_post(root, FakeCrawler(), post, comments)
            text = next((root / "内容").glob("*.txt")).read_text(encoding="utf-8-sig")
        self.assertIn("1. 这是一条评论", text)
        self.assertNotIn("赞 8", text)

    def test_no_comment_mode_omits_comment_section(self):
        class FakeCrawler:
            @staticmethod
            def download_images(post, folder: Path):
                folder.mkdir(parents=True, exist_ok=True)
                return []

        post = {"id": "456", "content": "只保存正文", "images": []}
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            save_post(root, FakeCrawler(), post, None)
            text = next((root / "内容").glob("*.txt")).read_text(encoding="utf-8-sig")
        self.assertEqual(text, "只保存正文\n")
        self.assertNotIn("评论：", text)


if __name__ == "__main__":
    unittest.main()
