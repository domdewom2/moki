"""
Tests for Settings - persistent user-configurable values.
"""
import json
import pytest
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))

from moki.managers.settings import Settings, DEFAULT_AUTO_PAUSE_MINUTES, DEFAULT_PROGRESS_EXPIRY_HOURS
from moki.config import DEFAULT_ADMIN_PIN, PIN_LENGTH


@pytest.fixture
def settings_path(tmp_path):
    return tmp_path / 'settings.json'


class TestSettingsDefaults:
    def test_defaults_when_no_file(self, settings_path):
        s = Settings(path=settings_path)
        assert s.auto_pause_minutes == DEFAULT_AUTO_PAUSE_MINUTES
        assert s.progress_expiry_hours == DEFAULT_PROGRESS_EXPIRY_HOURS
        assert s.admin_pin == DEFAULT_ADMIN_PIN

    def test_auto_pause_timeout_in_seconds(self, settings_path):
        s = Settings(path=settings_path)
        assert s.auto_pause_timeout == DEFAULT_AUTO_PAUSE_MINUTES * 60


class TestSettingsPersistence:
    def test_cycle_auto_pause_persists(self, settings_path):
        s = Settings(path=settings_path)
        new_val = s.cycle_auto_pause()
        assert new_val != DEFAULT_AUTO_PAUSE_MINUTES

        s2 = Settings(path=settings_path)
        assert s2.auto_pause_minutes == new_val

    def test_cycle_progress_expiry_persists(self, settings_path):
        s = Settings(path=settings_path)
        new_val = s.cycle_progress_expiry()
        assert new_val != DEFAULT_PROGRESS_EXPIRY_HOURS

        s2 = Settings(path=settings_path)
        assert s2.progress_expiry_hours == new_val

    def test_full_cycle_wraps_around(self, settings_path):
        s = Settings(path=settings_path)
        first = s.auto_pause_minutes
        from moki.managers.settings import AUTO_PAUSE_OPTIONS
        for _ in range(len(AUTO_PAUSE_OPTIONS)):
            s.cycle_auto_pause()
        assert s.auto_pause_minutes == first


class TestAdminPin:
    def test_default_pin(self, settings_path):
        s = Settings(path=settings_path)
        assert s.admin_pin == DEFAULT_ADMIN_PIN

    def test_set_admin_pin_persists(self, settings_path):
        s = Settings(path=settings_path)
        s.set_admin_pin('4321')
        assert s.admin_pin == '4321'
        s2 = Settings(path=settings_path)
        assert s2.admin_pin == '4321'

    def test_invalid_pin_rejected(self, settings_path):
        s = Settings(path=settings_path)
        with pytest.raises(ValueError):
            s.set_admin_pin('12')
        with pytest.raises(ValueError):
            s.set_admin_pin('abcd')

    def test_admin_pin_saved_in_json(self, settings_path):
        s = Settings(path=settings_path)
        s.set_admin_pin('9876')
        data = json.loads(settings_path.read_text())
        assert data['admin_pin'] == '9876'

    def test_invalid_stored_pin_uses_default(self, settings_path):
        settings_path.write_text(json.dumps({'admin_pin': 'bad'}))
        s = Settings(path=settings_path)
        assert s.admin_pin == DEFAULT_ADMIN_PIN


class TestShareUsageData:
    def test_default_is_true(self, settings_path):
        s = Settings(path=settings_path)
        assert s.share_usage_data is True

    def test_loads_false_from_file(self, settings_path):
        settings_path.write_text(json.dumps({'share_usage_data': False}))
        s = Settings(path=settings_path)
        assert s.share_usage_data is False

    def test_loads_true_from_file(self, settings_path):
        settings_path.write_text(json.dumps({'share_usage_data': True}))
        s = Settings(path=settings_path)
        assert s.share_usage_data is True

    def test_persisted_on_save(self, settings_path):
        settings_path.write_text(json.dumps({'share_usage_data': False}))
        s = Settings(path=settings_path)
        # Trigger a save via another setting change
        s.cycle_auto_pause()
        data = json.loads(settings_path.read_text())
        assert data['share_usage_data'] is False

    def test_missing_key_defaults_true(self, settings_path):
        settings_path.write_text(json.dumps({'auto_pause_minutes': 60}))
        s = Settings(path=settings_path)
        assert s.share_usage_data is True


class TestSettingsCorruption:
    def test_corrupted_file_uses_defaults(self, settings_path):
        settings_path.write_text('not json')
        s = Settings(path=settings_path)
        assert s.auto_pause_minutes == DEFAULT_AUTO_PAUSE_MINUTES

    def test_partial_file_uses_available(self, settings_path):
        settings_path.write_text(json.dumps({'auto_pause_minutes': 60}))
        s = Settings(path=settings_path)
        assert s.auto_pause_minutes == 60
        assert s.progress_expiry_hours == DEFAULT_PROGRESS_EXPIRY_HOURS
