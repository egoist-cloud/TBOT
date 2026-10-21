# -*- coding: utf-8 -*-
"""Общие настройки тестов.

Файл импортируется pytest ДО тестовых модулей, поэтому переменные
окружения выставляются здесь — до того, как импортируется script.py
и quantum.db. Иначе тесты читали бы (и перезаписывали) реальные
рабочие файлы данных.
"""
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Папка данных — всегда временная (TEST_DATA_DIR можно задать явно).
os.environ["DATA_DIR"] = os.environ.get("TEST_DATA_DIR") or tempfile.mkdtemp(
    prefix="quantum-tests-")

# Фейковые админские куки cs2red: тесты не зависят от реального
# окружения и ничего не отправляют наружу.
os.environ.setdefault("CS2RED_TOKEN", "CS2RED_ADMIN_SECRET")
os.environ.setdefault("CS2RED_BH", "BHSECRET")
os.environ.setdefault("CS2RED_I", "ISECRET")
os.environ.setdefault("STEAM_API_KEY", "STEAM_SECRET_KEY")

# Бот не должен пытаться подключаться к Discord/GitHub в тестах.
os.environ.setdefault("DISCORD_TOKEN", "")
os.environ.setdefault("GITHUB_TOKEN", "")
os.environ.setdefault("GITHUB_REPO", "")
os.environ.setdefault("GUILD_ID", "0")
