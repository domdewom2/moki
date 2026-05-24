"""
Tests for librespot hang recovery.
"""
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from moki.managers.librespot_recovery import LibrespotRecoveryManager


@pytest.fixture
def recovery():
    api = MagicMock()
    api.is_connected.return_value = True
    api.status.return_value = {'context_uri': 'spotify:album:1'}
    on_before = MagicMock()
    on_after = MagicMock()
    on_toast = MagicMock()
    has_network = MagicMock(return_value=True)
    mgr = LibrespotRecoveryManager(
        api=api,
        has_network_fn=has_network,
        on_before_restart=on_before,
        on_after_restart=on_after,
        on_toast=on_toast,
        mock_mode=False,
    )
    return mgr, api, on_before, on_after, on_toast, has_network


def test_connection_lost_requires_fail_threshold(recovery):
    mgr, _, _, _, _, has_network = recovery
    assert mgr.should_restart_for_connection(False, 7) is False
    assert mgr.should_restart_for_connection(False, 8) is True
    has_network.return_value = False
    assert mgr.should_restart_for_connection(False, 10) is False


def test_context_stall_trigger(recovery):
    mgr, _, _, _, _, _ = recovery
    assert mgr.should_restart_for_context_stall(19.0, None, True) is False
    assert mgr.should_restart_for_context_stall(20.0, None, True) is True
    assert mgr.should_restart_for_context_stall(25.0, 'spotify:album:x', True) is False


def test_play_timeout_cascade(recovery):
    mgr, _, _, _, _, _ = recovery
    now = time.time()
    mgr._play_timeout_times.extend([now - 10, now - 5, now - 1])
    assert mgr.should_restart_for_timeouts() is True


def test_cooldown_blocks_second_restart(recovery):
    mgr, _, on_before, on_after, on_toast, _ = recovery
    mgr._last_restart_at = time.time()

    assert mgr.maybe_restart('connection_lost') is False
    on_before.assert_not_called()
    on_toast.assert_not_called()


@patch('moki.managers.librespot_recovery.subprocess.run')
def test_restart_runs_stop_start_and_callbacks(mock_run, recovery):
    mgr, api, on_before, on_after, on_toast, _ = recovery
    mock_run.return_value = MagicMock(returncode=0)

    assert mgr.maybe_restart('connection_lost') is True
    on_before.assert_called_once()
    on_toast.assert_called_once()

    # Wait for background thread
    deadline = time.time() + 2.0
    while mgr._in_progress and time.time() < deadline:
        time.sleep(0.05)

    assert mock_run.call_count == 2
    on_after.assert_called_once_with(True)
    assert api.is_connected.called


def test_note_transport_failure_tracks_play_and_seek(recovery):
    mgr, _, _, _, _, _ = recovery
    mgr.note_transport_failure('pause')
    assert len(mgr._play_timeout_times) == 0
    mgr.note_transport_failure('play')
    mgr.note_transport_failure('seek')
    assert len(mgr._play_timeout_times) == 2
