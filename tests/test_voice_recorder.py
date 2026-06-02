"""
Tests for Sprachtest voice recorder.
"""
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

pygame_stub = SimpleNamespace(
    Surface=object,
    Rect=object,
    font=SimpleNamespace(Font=object),
)
sys.modules.setdefault('pygame', pygame_stub)
sys.modules.setdefault('pygame.gfxdraw', SimpleNamespace())

from moki.controllers.voice_recorder import VoiceRecorderController
from moki.ui.renderer import Renderer


@pytest.fixture
def voice_paths(tmp_path, monkeypatch):
    out = tmp_path / 'voice_test' / 'last.mp3'
    monkeypatch.setattr('moki.controllers.voice_recorder.VOICE_TEST_LAST_PATH', out)
    monkeypatch.setattr('moki.controllers.voice_recorder.VOICE_TEST_MAX_SECONDS', 3)
    return out


class TestVoiceRecorderController:
    def test_has_recording_false_when_missing(self, voice_paths):
        rec = VoiceRecorderController(mock_mode=True, output_path=voice_paths)
        assert rec.has_recording() is False

    def test_mock_record_and_stop(self, voice_paths):
        completed = []
        rec = VoiceRecorderController(
            mock_mode=True,
            output_path=voice_paths,
            on_recording_complete=lambda: completed.append(None),
        )
        assert rec.begin_recording() is True
        assert rec.is_recording is True
        assert rec.stop_recording() is True
        time.sleep(0.05)
        assert rec.is_encoding is False
        assert rec.has_recording() is True
        assert completed == [None]

    def test_auto_stop_at_max_duration(self, voice_paths):
        rec = VoiceRecorderController(mock_mode=True, output_path=voice_paths)
        rec.begin_recording()
        with patch.object(rec, 'stop_recording', wraps=rec.stop_recording) as stop_mock:
            rec._recording_started_at = time.time() - 4
            rec.tick()
            stop_mock.assert_called_once()

    def test_toggle_recording_mock(self, voice_paths):
        rec = VoiceRecorderController(mock_mode=True, output_path=voice_paths)
        assert rec.toggle_recording() is True
        assert rec.is_recording is True
        assert rec.toggle_recording() is True
        time.sleep(0.05)
        assert rec.is_recording is False

    def test_begin_shows_preparing_before_worker_finishes(self, voice_paths, monkeypatch):
        monkeypatch.setattr('moki.controllers.voice_recorder.sys.platform', 'linux')
        rec = VoiceRecorderController(mock_mode=False, output_path=voice_paths)
        gate = __import__('threading').Event()

        def slow_prepare():
            gate.wait(timeout=2)

        rec._on_before_record = slow_prepare
        with patch('moki.controllers.voice_recorder.shutil.which', return_value='/usr/bin/x'):
            with patch('moki.controllers.voice_recorder.subprocess.Popen', return_value=MagicMock(stderr=MagicMock())):
                with patch('moki.controllers.voice_recorder.get_wm8960_capture_device', return_value='plughw:1,0'):
                    with patch('moki.controllers.voice_recorder.prepare_wm8960_capture'):
                        with patch('moki.controllers.voice_recorder.mute_wm8960_output'):
                            with patch('moki.controllers.voice_recorder.time.sleep'):
                                assert rec.begin_recording() is True
                                assert rec.is_preparing is True
                                gate.set()
                                rec._start_thread.join(timeout=2)
        assert rec.is_recording is True

    def test_start_requires_tools_on_linux(self, voice_paths, monkeypatch):
        monkeypatch.setattr('moki.controllers.voice_recorder.sys.platform', 'linux')
        rec = VoiceRecorderController(mock_mode=False, output_path=voice_paths)
        errors = []
        rec._on_error = errors.append
        with patch('moki.controllers.voice_recorder.shutil.which', return_value=None):
            assert rec.begin_recording() is True
            rec._start_thread.join(timeout=2)
        assert errors
        assert rec.is_preparing is False

    def test_pipeline_start_linux(self, voice_paths, monkeypatch):
        monkeypatch.setattr('moki.controllers.voice_recorder.sys.platform', 'linux')
        rec = VoiceRecorderController(mock_mode=False, output_path=voice_paths)
        arecord = MagicMock()
        arecord.stderr = MagicMock()
        with patch('moki.controllers.voice_recorder.shutil.which', return_value='/usr/bin/x'):
            with patch('moki.controllers.voice_recorder.subprocess.Popen', return_value=arecord) as popen_mock:
                with patch('moki.controllers.voice_recorder.get_wm8960_capture_device', return_value='plughw:1,0'):
                    with patch('moki.controllers.voice_recorder.prepare_wm8960_capture'):
                        with patch('moki.controllers.voice_recorder.mute_wm8960_output'):
                            with patch('moki.controllers.voice_recorder.time.sleep'):
                                assert rec.begin_recording() is True
                                rec._start_thread.join(timeout=2)
        assert rec.is_recording is True
        assert popen_mock.call_count == 1
        assert popen_mock.call_args[0][0][0] == 'arecord'


class TestVoiceTestMenu:
    def test_main_menu_has_sprachtest_button(self):
        renderer = Renderer.__new__(Renderer)
        renderer.font_medium = MagicMock(return_value=MagicMock())
        renderer.font_large = MagicMock(return_value=MagicMock())
        ctx = SimpleNamespace(
            auto_pause_minutes=30,
            progress_expiry_hours=96,
            update_running=False,
            update_checking=False,
            update_available=False,
            shutdown_confirm_pending=False,
            reboot_confirm_pending=False,
            reset_confirm_pending=False,
            app_version_label='main@test',
        )
        items = Renderer._build_main_content(renderer, ctx)
        button_ids = [item[1] for item in items if item[0] == 'button']
        assert 'voice_test' in button_ids

    def test_voice_test_content_recording_state(self):
        renderer = Renderer.__new__(Renderer)
        ctx = SimpleNamespace(
            voice_recording=True,
            voice_preparing=False,
            voice_encoding=False,
            voice_playing=False,
            voice_has_recording=False,
            voice_recording_elapsed=12,
        )
        items = Renderer._build_voice_test_content(renderer, ctx)
        texts = [item[1] for item in items if item[0] == 'text']
        buttons = [(item[1], item[2]) for item in items if item[0] == 'button']
        assert any('Aufnahme' in t for t in texts)
        assert ('voice_record', 'Stop') in buttons

    def test_voice_test_content_preparing_state(self):
        renderer = Renderer.__new__(Renderer)
        ctx = SimpleNamespace(
            voice_recording=False,
            voice_preparing=True,
            voice_encoding=False,
            voice_playing=False,
            voice_has_recording=False,
            voice_recording_elapsed=0,
            voice_transcript=None,
            voice_transcribing=False,
            voice_transcribe_error=None,
        )
        items = Renderer._build_voice_test_content(renderer, ctx)
        texts = [item[1] for item in items if item[0] == 'text']
        buttons = [item[1] for item in items if item[0] == 'button']
        assert any('Vorbereiten' in t for t in texts)
        assert 'voice_record' not in buttons

    def test_voice_test_shows_transcribe_button(self):
        renderer = Renderer.__new__(Renderer)
        ctx = SimpleNamespace(
            voice_recording=False,
            voice_preparing=False,
            voice_encoding=False,
            voice_playing=False,
            voice_has_recording=True,
            voice_recording_elapsed=0,
            voice_transcript=None,
            voice_transcribing=False,
            voice_transcribe_error=None,
        )
        items = Renderer._build_voice_test_content(renderer, ctx)
        button_ids = [item[1] for item in items if item[0] == 'button']
        assert 'voice_transcribe' in button_ids
        assert 'voice_play' in button_ids

    def test_voice_test_transcribing_state(self):
        renderer = Renderer.__new__(Renderer)
        ctx = SimpleNamespace(
            voice_recording=False,
            voice_preparing=False,
            voice_encoding=False,
            voice_playing=False,
            voice_has_recording=True,
            voice_recording_elapsed=0,
            voice_transcript=None,
            voice_transcribing=True,
            voice_transcribe_error=None,
        )
        items = Renderer._build_voice_test_content(renderer, ctx)
        texts = [item[1] for item in items if item[0] == 'text']
        buttons = [item[1] for item in items if item[0] == 'button']
        assert any('Transkribiere' in t for t in texts)
        assert 'voice_transcribe' not in buttons

    def test_voice_test_shows_transcript(self):
        renderer = Renderer.__new__(Renderer)
        ctx = SimpleNamespace(
            voice_recording=False,
            voice_preparing=False,
            voice_encoding=False,
            voice_playing=False,
            voice_has_recording=True,
            voice_recording_elapsed=0,
            voice_transcript='stitch hörspiel bitte',
            voice_transcribing=False,
            voice_transcribe_error=None,
        )
        items = Renderer._build_voice_test_content(renderer, ctx)
        texts = [item[1] for item in items if item[0] == 'text']
        assert any('Erkannt' in t for t in texts)
        assert 'stitch hörspiel bitte' in texts
