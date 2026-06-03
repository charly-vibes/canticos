#!/usr/bin/env python3
import os
import sys
from pathlib import Path

script = Path(__file__).with_name("llm-dashboard.py")
os.execvp("python3", ["python3", str(script), *sys.argv[1:]])
