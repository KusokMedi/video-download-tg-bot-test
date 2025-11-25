#!/usr/bin/env python3
"""
KusokMedi Bot - Main entry point
Точка входа для запуска бота
"""

import sys
from pathlib import Path

# Добавить src в path
sys.path.insert(0, str(Path(__file__).parent / "src"))

from bot import run_bot

if __name__ == "__main__":
    run_bot()
