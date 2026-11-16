"""Shared helpers: project paths, config loading, logging, metadata registry."""
from __future__ import annotations

import datetime as dt
import json
import logging
import os
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent


def load_yaml(name: str) -> dict:
    with open(ROOT / "config" / name, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


SETTINGS = load_yaml("settings.yaml")


def path(key: str, *parts: str) -> Path:
    """Resolve a configured path key (raw, processed, ...) under the project root."""
    p = ROOT / SETTINGS["paths"][key]
    p = p.joinpath(*parts) if parts else p
    return p


def ensure_dir(p: Path) -> Path:
    p.mkdir(parents=True, exist_ok=True)
    return p


def get_logger(name: str) -> logging.Logger:
    logger = logging.getLogger(name)
    if not logger.handlers:
        logger.setLevel(logging.INFO)
        fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
        sh = logging.StreamHandler()
        sh.setFormatter(fmt)
        logger.addHandler(sh)
        log_dir = ensure_dir(path("metadata") / "logs")
        fh = logging.FileHandler(log_dir / "pipeline.log")
        fh.setFormatter(fmt)
        logger.addHandler(fh)
        logger.propagate = False
    return logger


def register_dataset(name: str, **info: Any) -> None:
    """Upsert a dataset entry in data/metadata/datasets.json (the data catalogue).

    Expected keys: source_url, retrieval_date, coverage_start, coverage_end, n_records,
    missingness, quality_score (0-1), limitations, path.
    """
    meta_file = ensure_dir(path("metadata")) / "datasets.json"
    catalogue = {}
    if meta_file.exists():
        catalogue = json.loads(meta_file.read_text())
    info.setdefault("retrieval_date", dt.date.today().isoformat())
    catalogue[name] = {**catalogue.get(name, {}), **info}
    meta_file.write_text(json.dumps(catalogue, indent=2, default=str, sort_keys=True))


def env_flag(name: str, default: bool = False) -> bool:
    v = os.environ.get(name)
    return default if v is None else v.lower() in {"1", "true", "yes"}
