"""Корень репозитория для тестов, читающих исходники и файлы по пути.

Один источник вместо `Path(__file__).resolve().parent.parent` в каждом файле: когда тесты
разъедутся по подпапкам, путь к корню не сломается."""
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
