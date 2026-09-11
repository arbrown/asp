import json
import unittest
from unittest.mock import MagicMock, patch

from storybook.api.routes import (
    _ART_STYLE_SEEDS,
    _MAX_LUCKY_HISTORY,
    _MOOD_SEEDS,
    _TRADITION_SEEDS,
    _build_lucky_prompt,
    _generate_lucky,
    _lucky_history,
    _shuffle,
    ShuffleRequest,
)


class TestLuckyAndShuffle(unittest.TestCase):
    def test_build_lucky_prompt_dynamic_and_exclusions(self):
        prompt1 = _build_lucky_prompt()
        self.assertIn("CRITICAL EXCLUSIONS", prompt1)
        self.assertIn("CREATIVE CATALYSTS FOR THIS RUN", prompt1)
        self.assertIn("Rudyard Kipling", prompt1)
        self.assertIn("Soviet constructivist", prompt1)

        # Check that seeds from our catalogs are present
        has_style = any(style in prompt1 for style in _ART_STYLE_SEEDS)
        has_tradition = any(trad in prompt1 for trad in _TRADITION_SEEDS)
        has_mood = any(mood in prompt1 for mood in _MOOD_SEEDS)
        self.assertTrue(has_style, "Prompt must include sampled art style seeds")
        self.assertTrue(has_tradition, "Prompt must include sampled tradition seeds")
        self.assertTrue(has_mood, "Prompt must include sampled mood seed")

        # Two consecutive prompt builds should produce different catalysts due to sampling
        prompts = {_build_lucky_prompt() for _ in range(5)}
        self.assertGreater(len(prompts), 1, "Prompts should vary across calls")

    def test_lucky_history_recording(self):
        fake_output = {
            "title": "The Golden Bird",
            "author": "Brothers Grimm",
            "target_age": "6-7",
            "page_count": 14,
            "text_spec": "Simple prose",
            "image_spec": "Luminous stained glass with leaded outlines and vibrant amber jewel tones",
            "custom_instructions": "Focus on the fox's loyalty and guidance.",
        }

        mock_response = MagicMock()
        mock_response.text = json.dumps(fake_output)

        mock_client = MagicMock()
        mock_client.models.generate_content.return_value = mock_response

        with patch("google.genai.Client", return_value=mock_client):
            res = _generate_lucky()
            self.assertEqual(res["title"], "The Golden Bird")
            self.assertEqual(res["author"], "Brothers Grimm")

            # Verify it was added to recency history
            self.assertEqual(_lucky_history[-1]["title"], "The Golden Bird")
            self.assertEqual(_lucky_history[-1]["author"], "Brothers Grimm")

            # Check that subsequent prompt includes this new entry in exclusions
            next_prompt = _build_lucky_prompt()
            self.assertIn("The Golden Bird", next_prompt)
            self.assertIn("Brothers Grimm", next_prompt)

    def test_shuffle_image_spec_uses_dynamic_seeds(self):
        req = ShuffleRequest(field="image_spec", title="Test Book", author="Test Author")

        captured_prompts = []

        def mock_generate_content(*args, **kwargs):
            captured_prompts.append(kwargs.get("contents") or args[0])
            resp = MagicMock()
            resp.text = json.dumps({"value": "A stylized papercraft diorama illustration"})
            return resp

        mock_client = MagicMock()
        mock_client.models.generate_content.side_effect = mock_generate_content

        with patch("google.genai.Client", return_value=mock_client):
            res = _shuffle(req)
            self.assertEqual(res.image_spec, "A stylized papercraft diorama illustration")
            self.assertTrue(len(captured_prompts) > 0)
            prompt = captured_prompts[0]
            # Ensure static 'constructivist' was replaced with dynamic seeds
            self.assertTrue(any(seed in prompt for seed in _ART_STYLE_SEEDS))
