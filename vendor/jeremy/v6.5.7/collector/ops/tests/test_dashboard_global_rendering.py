#!/usr/bin/env python3

import pathlib
import sys
import unittest

OPS_DIR = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(OPS_DIR))

import dashboard  # noqa: E402


class DashboardGlobalRenderingTests(unittest.TestCase):
    def global_section(self) -> str:
        html = dashboard.HTML
        start = html.index('<section id="tab-global"')
        end = html.index('<section id="tab-earnings"', start)
        return html[start:end]

    def test_global_producer_trend_defaults_to_blocks_per_hour(self) -> None:
        section = self.global_section()

        self.assertIn("Producer Trend", section)
        self.assertIn("Blocks produced per producer address per hour", section)
        self.assertIn('let globalChartMetric = "blocks";', dashboard.HTML)
        self.assertIn(
            '<button class="secondary range-button global-metric-button" data-metric="usd" onclick="setGlobalChartMetric(\'usd\')">USD/h</button>',
            section,
        )
        self.assertIn(
            '<button class="secondary range-button global-metric-button active" data-metric="blocks" onclick="setGlobalChartMetric(\'blocks\')">Blocks/h</button>',
            section,
        )
        self.assertNotIn(
            '<button class="secondary range-button global-metric-button active" data-metric="usd"',
            section,
        )


if __name__ == "__main__":
    unittest.main()
