#!/usr/bin/env python3
"""Run LegacyProxy: python3 proxy.py [options] (see --help)."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from legacyproxy.__main__ import main  # noqa: E402

main()
