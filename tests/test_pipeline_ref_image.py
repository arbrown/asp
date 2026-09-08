import asyncio
import unittest
from unittest.mock import AsyncMock, patch, MagicMock

from storybook.models import IllustrationEntry, SpreadContent, SpreadPlan
from storybook.agents.pipeline import _generate_with_retries


class TestPipelineRefImage(unittest.IsolatedAsyncioTestCase):
    async def test_generate_with_retries_is_ref_image_does_not_wait(self):
        ref_ready = asyncio.Event()
        ref_image = []
        progress_queue = asyncio.Queue()

        spread_content = SpreadContent(
            spread_number=1,
            verso_text="text",
            recto_text="text",
        )
        illustration_entry = IllustrationEntry(
            image_index=0,
            coverage="full",
            aspect_ratio="16:9",
            illustration_notes="notes",
        )

        with (
            patch("storybook.agents.pipeline.generate_image", return_value=b"fake_image_bytes"),
            patch("storybook.agents.pipeline._run_agent", new_callable=AsyncMock) as mock_run_agent,
            patch("storybook.agents.pipeline._make_runner", return_value=MagicMock()),
        ):
            mock_run_agent.return_value = "approved"

            img_bytes = await _generate_with_retries(
                session_id="test-session",
                spread_number=1,
                image_index=0,
                image_prompt="test prompt",
                spread_content=spread_content,
                illustration_entry=illustration_entry,
                bible_dict={},
                image_sem=asyncio.Semaphore(2),
                llm_sem=asyncio.Semaphore(2),
                ref_ready=ref_ready,
                ref_image=ref_image,
                progress_queue=progress_queue,
                completed_spread_images={},
                is_ref_image=True,
            )

            self.assertEqual(img_bytes, b"fake_image_bytes")
            # Should have succeeded without ref_ready ever being set!
            self.assertFalse(ref_ready.is_set())

    async def test_generate_with_retries_non_ref_waits_for_ref_ready(self):
        ref_ready = asyncio.Event()
        ref_image = []
        progress_queue = asyncio.Queue()

        spread_content = SpreadContent(
            spread_number=2,
            verso_text="text",
            recto_text="text",
        )
        illustration_entry = IllustrationEntry(
            image_index=0,
            coverage="full",
            aspect_ratio="16:9",
            illustration_notes="notes",
        )

        with (
            patch("storybook.agents.pipeline.generate_image", return_value=b"fake_image_bytes_2"),
            patch("storybook.agents.pipeline._run_agent", new_callable=AsyncMock) as mock_run_agent,
            patch("storybook.agents.pipeline._make_runner", return_value=MagicMock()),
        ):
            mock_run_agent.return_value = "approved"

            task = asyncio.create_task(
                _generate_with_retries(
                    session_id="test-session",
                    spread_number=2,
                    image_index=0,
                    image_prompt="test prompt",
                    spread_content=spread_content,
                    illustration_entry=illustration_entry,
                    bible_dict={},
                    image_sem=asyncio.Semaphore(2),
                    llm_sem=asyncio.Semaphore(2),
                    ref_ready=ref_ready,
                    ref_image=ref_image,
                    progress_queue=progress_queue,
                    completed_spread_images={},
                    is_ref_image=False,
                )
            )

            # Give it a moment to run and block on ref_ready.wait()
            await asyncio.sleep(0.01)
            self.assertFalse(task.done())

            # Now signal ref_ready
            ref_image.append(b"anchor_style_image")
            ref_ready.set()

            result = await asyncio.wait_for(task, timeout=1.0)
            self.assertEqual(result, b"fake_image_bytes_2")
            # Verify reference_image was passed to validator
            _, kwargs = mock_run_agent.call_args
            self.assertEqual(kwargs.get("reference_image"), b"anchor_style_image")


if __name__ == "__main__":
    unittest.main()


class TestFirstImageCoord(unittest.TestCase):
    def test_first_image_coord_spread_0_empty(self):
        spread_contents = [
            SpreadContent(spread_number=0),
            SpreadContent(spread_number=1),
            SpreadContent(spread_number=2),
        ]
        plan_by_spread = {
            0: SpreadPlan(spread_number=0, illustration_plan=[]),
            1: SpreadPlan(
                spread_number=1,
                illustration_plan=[IllustrationEntry(image_index=0, coverage="full", aspect_ratio="16:9", illustration_notes="")],
            ),
            2: SpreadPlan(
                spread_number=2,
                illustration_plan=[IllustrationEntry(image_index=0, coverage="full", aspect_ratio="16:9", illustration_notes="")],
            ),
        }

        first_image_coord = None
        for sc in sorted(spread_contents, key=lambda c: c.spread_number):
            plan = plan_by_spread.get(sc.spread_number)
            if plan and plan.illustration_plan:
                first_image_coord = (sc.spread_number, plan.illustration_plan[0].image_index)
                break

        self.assertEqual(first_image_coord, (1, 0))

    def test_first_image_coord_spread_0_has_image(self):
        spread_contents = [
            SpreadContent(spread_number=0),
            SpreadContent(spread_number=1),
        ]
        plan_by_spread = {
            0: SpreadPlan(
                spread_number=0,
                illustration_plan=[IllustrationEntry(image_index=0, coverage="verso", aspect_ratio="3:4", illustration_notes="")],
            ),
            1: SpreadPlan(
                spread_number=1,
                illustration_plan=[IllustrationEntry(image_index=0, coverage="full", aspect_ratio="16:9", illustration_notes="")],
            ),
        }

        first_image_coord = None
        for sc in sorted(spread_contents, key=lambda c: c.spread_number):
            plan = plan_by_spread.get(sc.spread_number)
            if plan and plan.illustration_plan:
                first_image_coord = (sc.spread_number, plan.illustration_plan[0].image_index)
                break

        self.assertEqual(first_image_coord, (0, 0))

    def test_first_image_coord_no_images(self):
        spread_contents = [SpreadContent(spread_number=0)]
        plan_by_spread = {0: SpreadPlan(spread_number=0, illustration_plan=[])}

        first_image_coord = None
        for sc in sorted(spread_contents, key=lambda c: c.spread_number):
            plan = plan_by_spread.get(sc.spread_number)
            if plan and plan.illustration_plan:
                first_image_coord = (sc.spread_number, plan.illustration_plan[0].image_index)
                break

        self.assertIsNone(first_image_coord)
