"""WiFi link info helpers (parse iw/nmcli output for settings UI)."""
from typing import Optional, Tuple


def format_frequency_label(mhz: Optional[int]) -> str:
    if not mhz:
        return 'unknown'
    if mhz < 3000:
        return '2.4 GHz'
    return '5 GHz'


def parse_iw_link_output(text: str) -> Tuple[Optional[int], Optional[int], Optional[str]]:
    """Return (frequency_mhz, signal_dbm, ssid) from ``iw dev wlan0 link`` output."""
    freq = signal = ssid = None
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith('freq:'):
            try:
                freq = int(stripped.split(':', 1)[1].strip().split()[0])
            except (IndexError, ValueError):
                pass
        elif stripped.startswith('signal:'):
            try:
                signal = int(stripped.split(':', 1)[1].strip().split()[0])
            except (IndexError, ValueError):
                pass
        elif stripped.startswith('SSID:'):
            ssid = stripped.split(':', 1)[1].strip()
    return freq, signal, ssid


def nmcli_band_to_mode(band: str) -> str:
    """Map NetworkManager band setting to UI mode key."""
    normalized = (band or '').strip().lower()
    if normalized == 'bg':
        return '2.4'
    if normalized == 'a':
        return '5'
    return 'auto'


def mode_to_nmcli_band(mode: str) -> Optional[str]:
    """Map UI mode key to NetworkManager band value (None = auto)."""
    if mode == '2.4':
        return 'bg'
    if mode == '5':
        return 'a'
    return None


def next_band_mode(current: str) -> str:
    order = ('2.4', 'auto', '5')
    try:
        idx = order.index(current)
    except ValueError:
        return '2.4'
    return order[(idx + 1) % len(order)]


def band_mode_label(mode: str) -> str:
    labels = {
        '2.4': 'Prefer: 2.4 GHz',
        'auto': 'Prefer: Auto',
        '5': 'Prefer: 5 GHz',
    }
    return labels.get(mode, labels['2.4'])


def format_link_status(
    ssid: Optional[str],
    freq_mhz: Optional[int],
    signal_dbm: Optional[int],
    connected: bool,
) -> str:
    if not connected:
        return 'Not connected'
    name = ssid or 'WiFi'
    band = format_frequency_label(freq_mhz)
    if signal_dbm is not None:
        return f'{name} · {band} ({signal_dbm} dBm)'
    return f'{name} · {band}'
