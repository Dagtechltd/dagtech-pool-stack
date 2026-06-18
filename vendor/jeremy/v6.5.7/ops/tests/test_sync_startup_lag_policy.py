import subprocess
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
POLICY = ROOT / "ops" / "sync-startup-lag-policy.sh"
CHAIN_PRESYNC = ROOT / "ops" / "chain-presync.sh"


def bash_eval(script: str) -> str:
    result = subprocess.run(
        ["bash", "-c", f"source {POLICY}; {script}"],
        check=True,
        text=True,
        capture_output=True,
    )
    return result.stdout.strip()


def test_default_startup_lag_floor_is_four_thousand_blocks() -> None:
    assert bash_eval("bdag_sync_lag_threshold_blocks '' '' '' ''") == "4000"


def test_duration_allowance_can_widen_above_floor() -> None:
    assert bash_eval("bdag_sync_lag_threshold_blocks 1 4 '' 1200") == "80"


def test_target_tip_min_tip_uses_acceptance_window() -> None:
    assert bash_eval("bdag_sync_min_tip_for_target 9747700 '' 4000 4 '' ''") == "9743700"


def test_explicit_min_tip_wins_over_target_tip_policy() -> None:
    assert bash_eval("bdag_sync_min_tip_for_target 9747700 9747600 4000 4 '' ''") == "9747600"


def test_copy_duration_file_is_recorded_and_read() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "copy-seconds"
        output = bash_eval(
            f"bdag_sync_record_copy_seconds {path} 1200; "
            f"bdag_sync_lag_threshold_blocks 1 4 {path} ''"
        )
    assert output == "80"


def test_presync_uses_shared_policy() -> None:
    presync = CHAIN_PRESYNC.read_text(encoding="utf-8")

    assert "sync-startup-lag-policy.sh" in presync
    assert "PRESYNC_ACCEPTABLE_BLOCK_LAG_FLOOR" in presync
    assert "bdag_sync_record_copy_seconds" in presync
