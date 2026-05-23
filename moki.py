#!/usr/bin/env python3
"""
Moki - Pygame UI for Raspberry Pi

This file is a backwards-compatible wrapper.
The actual implementation is now in the moki/ package.

Usage:
    python moki.py              # Windowed (development)
    python moki.py --fullscreen # Fullscreen (Pi)
    python moki.py --mock       # Mock mode (UI testing)
"""
from moki.main import main

if __name__ == '__main__':
    main()
