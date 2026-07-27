import unittest

from brain.context_ranker import ContextRanker


class ContextRankerTests(unittest.TestCase):
    def test_rank_prioritizes_relevant_items(self):
        ranker = ContextRanker(max_items=3, max_chars=500)
        items = [
            {"source": "web", "title": "Old blog", "snippet": "Python tutorial", "priority": 1},
            {"source": "official", "title": "Python docs", "snippet": "How to handle errors", "priority": 5},
            {"source": "memory", "title": "Project note", "snippet": "We are building a local agent", "priority": 4},
        ]

        ranked = ranker.rank(items)

        self.assertEqual(ranked[0]["source"], "official")
        self.assertGreaterEqual(ranked[0]["priority"], 10)
        self.assertLessEqual(len(ranked), 3)


if __name__ == "__main__":
    unittest.main()
