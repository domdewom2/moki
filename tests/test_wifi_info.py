from moki.utils.wifi_info import (
    band_mode_label,
    format_frequency_label,
    format_link_detail,
    format_link_status,
    format_network_button_label,
    format_now_band,
    next_band_mode,
    nmcli_band_to_mode,
    parse_iw_link_output,
)


def test_format_frequency_label():
    assert format_frequency_label(2437) == '2.4 GHz'
    assert format_frequency_label(5260) == '5 GHz'
    assert format_frequency_label(None) == 'unknown'


def test_parse_iw_link_output():
    sample = """
Connected to aa:bb:cc:dd:ee:ff (on wlan0)
    SSID: Grogu
    freq: 2437
    signal: -62 dBm
    rx bytes: 123
"""
    freq, signal, ssid = parse_iw_link_output(sample)
    assert ssid == 'Grogu'
    assert freq == 2437
    assert signal == -62


def test_band_mode_cycle():
    assert nmcli_band_to_mode('bg') == '2.4'
    assert nmcli_band_to_mode('a') == '5'
    assert nmcli_band_to_mode('') == 'auto'
    assert next_band_mode('2.4') == 'auto'
    assert next_band_mode('auto') == '5'
    assert next_band_mode('5') == '2.4'
    assert band_mode_label('auto') == 'Prefer: Auto'


def test_format_link_status():
    assert format_link_status('Grogu', 2437, -65, True) == 'Grogu · 2.4 GHz (-65 dBm)'
    assert format_link_status(None, None, None, False) == 'Not connected'


def test_format_now_band_and_detail():
    assert format_now_band(True, 2437) == '2.4 GHz'
    assert format_now_band(True, 5260) == '5 GHz'
    assert format_now_band(False, 2437) == 'Not connected'
    assert format_link_detail('Grogu', -65, True) == 'Grogu · -65 dBm'


def test_format_network_button_label():
    assert format_network_button_label('Grogu', True, '2.4 GHz') == 'Grogu · 2.4 GHz'
    assert format_network_button_label('Grogu', False, '2.4 GHz') == 'Grogu'
