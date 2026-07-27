import unittest

from brain.source_validator import SourceValidator


class SourceValidatorTests(unittest.TestCase):
    def test_scores_official_sources_higher_than_random_blogs(self):
        validator = SourceValidator()
        official = {"title": "Python docs", "url": "https://docs.python.org/3/whatsnew/3.14.html", "snippet": "official documentation"}
        blog = {"title": "My random blog", "url": "https://example.com/blog/python", "snippet": "some thoughts"}

        official_score = validator.score(official)
        blog_score = validator.score(blog)

        self.assertGreater(official_score, blog_score)
        self.assertEqual(validator.enrich([official, blog])[0]["source_score"], official_score)


if __name__ == "__main__":
    unittest.main()
