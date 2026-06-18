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

    def test_global_table_uses_chain_blocks_and_hides_removed_columns(self) -> None:
        section = self.global_section()

        self.assertNotIn('<th class="right">Shares In Window</th>', section)
        self.assertNotIn('<th class="nowrap">Nodes</th>', section)
        self.assertIn('<table class="wide-table equal-column-table">', section)
        self.assertIn(".equal-column-table", dashboard.HTML)
        self.assertIn("table-layout: fixed;", dashboard.HTML)
        self.assertIn('<th class="right">Chain Blocks In Window</th>', section)
        self.assertLess(
            section.index('<th class="right">Chain Blocks In Window</th>'),
            section.index('<th class="right">Work %</th>'),
        )
        self.assertNotIn('<th class="right">Avg USD/h</th>', section)
        self.assertNotIn('<th class="right">Wallet Avg BDAG/h</th>', section)
        self.assertNotIn('<th class="right">Credit Blocks</th>', section)
        self.assertNotIn('<th class="right">Found Blocks</th>', section)
        self.assertIn('id="globalTableWindow"', section)
        self.assertIn("Table period: waiting for scan window.", section)
        self.assertIn(
            "Pool rows use chain-confirmed production over the displayed Scan Window.",
            section,
        )
        self.assertIn("Credit-block and found-block duplicates are intentionally hidden", section)

    def test_global_rows_render_chain_blocks_without_removed_columns(self) -> None:
        html = dashboard.HTML

        self.assertIn("function formatGlobalTableWindow(data)", html)
        self.assertIn('text("globalTableWindow", formatGlobalTableWindow(data));', html)
        self.assertIn("const chainBlocks = firstPresent(row.blocks, row.found_blocks);", html)
        self.assertNotIn("const shares = firstPresent(row.shares, row.blocks);", html)
        self.assertNotIn("const avgUsd = firstPresent(row.estimated_usd_avg_hour, row.estimated_usd_recent_hour);", html)
        self.assertNotIn("const avgBdag = firstPresent(row.estimated_bdag_avg_hour, row.estimated_bdag_recent_hour);", html)
        self.assertNotIn("const nodes = globalNodesLabel(row);", html)
        self.assertNotIn("const shares = row.blocks;", html)
        self.assertIn('colspan="8"', html)
        self.assertNotIn('colspan="9"', html)

    def test_miners_table_filters_stale_inactive_inventory_rows(self) -> None:
        html = dashboard.HTML

        self.assertIn("Active Miner Lanes", html)
        self.assertIn("function activeMinerLaneRow(miner)", html)
        self.assertIn("function localAsicMinerLaneRow(miner)", html)
        self.assertIn('String(miner.device_type || "").toLowerCase() === "asic"', html)
        self.assertIn("const rows = allRows.filter(localAsicMinerLaneRow);", html)
        self.assertIn("hidden-non-asic-or-inactive=", html)
        self.assertIn("stratum-hidden=", html)
        self.assertIn("No active local ASIC lanes are currently present.", html)
        self.assertNotIn("Tracked Miner Health", html)

    def test_status_tab_keeps_single_backend_header_in_one_row(self) -> None:
        html = dashboard.HTML
        start = html.index('<section id="tab-status"')
        end = html.index('<section class="grid">', start)
        section = html[start:end]

        self.assertIn("stack-summary-row", html)
        self.assertIn("height-summary", html)
        self.assertIn('id="syncHeight"', section)
        self.assertIn('id="syncActiveLabel"', section)
        self.assertIn(".status-overview.single-card", html)
        self.assertIn('overview.classList.toggle("single-card"', html)
        self.assertIn("singleManagedTopology || fallbackOnly", html)
        self.assertIn("Managed node is synced to the current network tip.", html)

    def test_status_tab_shows_mining_pause_state_in_overview(self) -> None:
        html = dashboard.HTML
        start = html.index('<section id="tab-status"')
        end = html.index('<section class="grid">', start)
        section = html[start:end]

        self.assertIn('id="syncMiningState"', section)
        self.assertIn('id="miningStateBox"', section)
        self.assertIn("Paused for chain catch-up", html)
        self.assertIn("the pool is not mining", html)
        self.assertIn("Stopped: node chain state is stuck on irreparable sync block", html)
        self.assertIn("Restore or resync node data before mining", html)
        self.assertIn("Synced node, but waiting for backend template checks to become healthy.", html)
        self.assertIn("wait for backend template checks to become healthy before mining jobs are sent", html)
        self.assertLess(
            section.index('id="syncHeight"'),
            section.index('id="syncMiningState"'),
        )

    def test_plot_refresh_and_sampler_defaults_are_one_minute(self) -> None:
        html = dashboard.HTML

        self.assertEqual(dashboard.EARNINGS_SAMPLER_INTERVAL_SECONDS, 60.0)
        self.assertEqual(dashboard.GLOBAL_SAMPLER_INTERVAL_SECONDS, 60.0)
        self.assertIn("setInterval(refresh, 60000);", html)
        self.assertIn(")) refreshEarnings();\n    }, 60000);", html)
        self.assertIn("refreshGlobal(); }, 60000);", html)
        self.assertIn("let earningsRefreshInFlight = false;", html)
        self.assertIn("let globalRefreshInFlight = false;", html)
        self.assertNotIn("refreshGlobal(); }, 300000);", html)

    def test_sync_estimate_uses_backend_next_step_when_present(self) -> None:
        html = dashboard.HTML

        self.assertIn("if (estimate.next_step) {", html)
        self.assertIn('text("syncNextStep", estimate.next_step);', html)
        self.assertLess(
            html.index("if (estimate.next_step) {"),
            html.index('text("syncNextStep", "pool can mine normally once backend template checks are healthy");'),
        )


if __name__ == "__main__":
    unittest.main()
