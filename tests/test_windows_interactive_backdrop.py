import unittest

from verify_windows_interactive_backdrop import (
    _backdrop_status_failure,
    _performance_failure_reason,
)


class InteractiveBackdropGateTests(unittest.TestCase):
    def test_single_scheduler_outlier_does_not_fail_performance_gate(self):
        values = [7.0] * 239 + [40.0]
        self.assertIsNone(_performance_failure_reason("resize", values))

    def test_sustained_one_frame_stalls_fail_p95_gate(self):
        values = [7.0] * 220 + [20.0] * 20
        self.assertIn(
            "p95-stall",
            _performance_failure_reason("resize", values),
        )

    def test_multiple_two_frame_stalls_fail_p99_gate(self):
        values = [7.0] * 237 + [40.0] * 3
        self.assertIn(
            "p99-stall",
            _performance_failure_reason("resize", values),
        )

    def test_backdrop_status_requires_acrylic_and_success(self):
        self.assertIsNone(
            _backdrop_status_failure(
                {
                    "backdrop_backend": "win10_effect_acrylic",
                    "backdrop_success": "True",
                }
            )
        )
        self.assertIn(
            "resting-backend",
            _backdrop_status_failure(
                {
                    "backdrop_backend": "win11_mica",
                    "backdrop_success": "True",
                }
            ),
        )
        self.assertIn(
            "backdrop-unsuccessful",
            _backdrop_status_failure(
                {
                    "backdrop_backend": "win10_effect_acrylic",
                    "backdrop_success": "False",
                }
            ),
        )


if __name__ == "__main__":
    unittest.main()
