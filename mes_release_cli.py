#!/usr/bin/env python3
"""
药品生产 MES 系统版本发布与智能回滚自动化管理平台 - 命令行入口
"""
import sys
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from mes_release.pipeline import main

if __name__ == "__main__":
    main()
