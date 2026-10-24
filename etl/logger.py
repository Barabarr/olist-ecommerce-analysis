"""Logging central do pipeline.

Substitui os `print()` espalhados pelo script original por um logger de
verdade: tem nível (INFO/WARNING/ERROR), timestamp, e pode ser redirecionado
para arquivo em produção sem tocar no resto do código.
"""
from __future__ import annotations

import logging
import sys


def get_logger(name: str = "olist_etl") -> logging.Logger:
    logger = logging.getLogger(name)
    if not logger.handlers:  # evita handlers duplicados em re-imports
        logger.setLevel(logging.INFO)
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(
            logging.Formatter("%(asctime)s | %(levelname)-7s | %(message)s", "%H:%M:%S")
        )
        logger.addHandler(handler)
        logger.propagate = False
    return logger
