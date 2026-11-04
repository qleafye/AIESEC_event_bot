"""Псевдонимы СПбГЭУ и ФИНЭК в справочнике вузов.

Делегаты пишут «СПбГЭУ» (и «финэк» в любом регистре), подсказка вуза не должна их терять.
Поиск вызывается реальный (`search_lookup`) на засеянной из снапшота базе, без моков.
"""
from __future__ import annotations

import asyncio

from config import config
from services.registration import lookup
from services.registration.lookup import search_lookup
from tests._dbtpl import fast_init_db

SPBGEU = "Санкт-Петербургский государственный экономический университет"


def _ready(tmp_path):
    config.DB_PATH = str(tmp_path / "university_alias_spbgeu.db")
    fast_init_db()
    lookup.invalidate_rank_cache()


def test_spbgeu_abbreviation_finds_canonical(tmp_path):
    _ready(tmp_path)
    result = asyncio.run(search_lookup("university", "СПбГЭУ", limit=5))
    assert result, "подсказка по «СПбГЭУ» пуста"
    assert result[0]["canonical"] == SPBGEU


def test_finek_lowercase_finds_same_canonical(tmp_path):
    _ready(tmp_path)
    result = asyncio.run(search_lookup("university", "финэк", limit=5))
    assert result, "подсказка по «финэк» пуста"
    assert result[0]["canonical"] == SPBGEU
