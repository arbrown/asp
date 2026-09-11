import unittest

from storybook.tools.gutenberg import (
    find_matching_gutenberg_candidate,
    is_exact_gutenberg_match,
)


class TestGutenbergMatching(unittest.TestCase):
    def test_exact_matches(self):
        cand1 = {
            "id": 146,
            "title": "A Little Princess: Being the whole story of Sara Crewe now told for the first time",
            "authors": ["Burnett, Frances Hodgson"],
        }
        cand2 = {
            "id": 1597,
            "title": "Andersen's Fairy Tales",
            "authors": ["Andersen, H. C. (Hans Christian)"],
        }
        cand3 = {
            "id": 9999,
            "title": "The Princess and the Pea",
            "authors": ["Andersen, H. C. (Hans Christian)"],
        }

        # Andersen's Princess on the Pea should NOT match A Little Princess
        self.assertFalse(
            is_exact_gutenberg_match("The Princess on the Pea", "Hans Christian Andersen", cand1)
        )
        self.assertFalse(
            is_exact_gutenberg_match("The Princess on the Pea", None, cand1)
        )

        # "The Princess on the Pea" should match "The Princess and the Pea"
        self.assertTrue(
            is_exact_gutenberg_match("The Princess on the Pea", "Hans Christian Andersen", cand3)
        )
        self.assertTrue(
            is_exact_gutenberg_match("The Princess on the Pea", None, cand3)
        )

    def test_find_matching_candidate_fallback(self):
        candidates = [
            {
                "id": 146,
                "title": "A Little Princess: Being the whole story of Sara Crewe now told for the first time",
                "authors": ["Burnett, Frances Hodgson"],
            },
            {
                "id": 28941,
                "title": "The Princess and the Goblin",
                "authors": ["MacDonald, George"],
            },
        ]

        # Neither matches "The Princess on the Pea"
        matched = find_matching_gutenberg_candidate(
            "The Princess on the Pea",
            "Hans Christian Andersen",
            candidates,
        )
        self.assertIsNone(matched)

        # If a true match is present later in the list, it should find it
        cand_match = {
            "id": 9999,
            "title": "The Princess and the Pea",
            "authors": ["Andersen, H. C. (Hans Christian)"],
        }
        candidates.append(cand_match)
        matched = find_matching_gutenberg_candidate(
            "The Princess on the Pea",
            "Hans Christian Andersen",
            candidates,
        )
        self.assertEqual(matched, cand_match)
