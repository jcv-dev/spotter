"""Offline city-coordinate lookup built from public US datasets.

Sources (downloaded on demand into a cache directory):

* **US Census Gazetteer** place + county-subdivision files (public domain) –
  covers incorporated places, CDPs and townships.
* **GeoNames** US dump (CC BY 4.0, optional via ``with_geonames=True``) – adds
  unincorporated communities and hamlets.

City names are normalised (case, punctuation, diacritics, Census LSAD suffixes
such as "city"/"CDP", and common abbreviations like ``St.``/``Saint`` or
``S``/``South``) so the CSV's city names match reliably. When a name exists
multiple times in a state the largest place wins.
"""

from __future__ import annotations

import csv
import logging
import re
import unicodedata
import zipfile
from pathlib import Path

import requests
from django.conf import settings

logger = logging.getLogger("fuel.city_lookup")

DATASET_YEAR = "2024"
CENSUS_BASE_URL = (
    f"https://www2.census.gov/geo/docs/maps-data/data/gazetteer/{DATASET_YEAR}_Gazetteer"
)
GEONAMES_US_URL = "https://download.geonames.org/export/dump/US.zip"

_DATASETS = {
    "place": (
        f"{CENSUS_BASE_URL}/{DATASET_YEAR}_Gaz_place_national.zip",
        f"{DATASET_YEAR}_Gaz_place_national.txt",
    ),
    "cousubs": (
        f"{CENSUS_BASE_URL}/{DATASET_YEAR}_Gaz_cousubs_national.zip",
        f"{DATASET_YEAR}_Gaz_cousubs_national.txt",
    ),
    "geonames": (GEONAMES_US_URL, "US.txt"),
}

_SUFFIX_RE = re.compile(
    r"\b(city|town|village|cdp|borough|municipality|urban county|township|plantation|"
    r"unified government|metro government|consolidated government|balance|county)\b"
)
_ALIASES = {
    "st": "st",
    "saint": "st",
    "ste": "st",
    "sainte": "st",
    "mt": "mt",
    "mount": "mt",
    "ft": "ft",
    "fort": "ft",
    "n": "north",
    "s": "south",
    "e": "east",
    "w": "west",
}


def default_cache_dir() -> Path:
    return Path.home() / ".cache" / "fuel-route"


def normalize_city(name: str) -> str:
    """Normalise a city name for matching (see module docstring)."""
    text = unicodedata.normalize("NFKD", name or "").encode("ascii", "ignore").decode("ascii")
    text = text.lower()
    text = re.sub(r"[.'’\-]", " ", text)
    text = _SUFFIX_RE.sub(" ", text)
    tokens = [token for token in re.split(r"[^a-z0-9]+", text) if token]
    tokens = [_ALIASES.get(token, token) for token in tokens]
    return "".join(tokens)


def _download(url: str, destination: Path) -> None:
    logger.info("Downloading %s", url)
    response = requests.get(
        url,
        stream=True,
        timeout=settings.ORS_TIMEOUT_SECONDS * 15,
        headers={"User-Agent": settings.MAPS_USER_AGENT},
    )
    response.raise_for_status()
    with destination.open("wb") as handle:
        for chunk in response.iter_content(chunk_size=1 << 20):
            handle.write(chunk)


def ensure_dataset(cache_dir: Path, key: str) -> Path:
    """Return the local text file for a dataset, downloading it if needed."""
    url, member = _DATASETS[key]
    text_path = cache_dir / member
    if text_path.exists():
        return text_path

    cache_dir.mkdir(parents=True, exist_ok=True)
    archive = cache_dir / f"{key}.zip"
    if not archive.exists():
        _download(url, archive)
    with zipfile.ZipFile(archive) as zf:
        with zf.open(member) as source, text_path.open("wb") as target:
            target.write(source.read())
    return text_path


def _read_gazetteer(path: Path) -> dict[tuple[str, str], tuple[int, float, float]]:
    """Parse a pipe/tab-delimited Census gazetteer file."""
    result: dict[tuple[str, str], tuple[int, float, float]] = {}
    with path.open(encoding="utf-8-sig") as handle:
        header = [column.strip() for column in handle.readline().rstrip("\n").split("\t")]
        for row in csv.DictReader(handle, fieldnames=header, delimiter="\t"):
            name = normalize_city(row["NAME"])
            state = row["USPS"].strip().upper()
            if not name:
                continue
            area = int((row["ALAND"] or "0").strip() or 0)
            key = (name, state)
            existing = result.get(key)
            if existing is None or area > existing[0]:
                result[key] = (area, float(row["INTPTLAT"]), float(row["INTPTLONG"]))
    return result


def _read_geonames(path: Path) -> dict[tuple[str, str], tuple[int, float, float]]:
    """Parse the GeoNames US dump, keeping populated places (feature class P)."""
    result: dict[tuple[str, str], tuple[int, float, float]] = {}
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            fields = line.rstrip("\n").split("\t")
            if len(fields) < 15 or fields[6] != "P" or fields[8] != "US":
                continue
            name = normalize_city(fields[1])
            state = fields[10].strip().upper()
            if not name:
                continue
            population = int(fields[14] or 0)
            key = (name, state)
            existing = result.get(key)
            if existing is None or population > existing[0]:
                result[key] = (population, float(fields[4]), float(fields[5]))
    return result


class CityLookup:
    """Normalized ``(city, state) -> (lat, lon)`` mapping."""

    def __init__(self, mapping: dict[tuple[str, str], tuple[float, float]]):
        self._mapping = mapping

    def __len__(self) -> int:
        return len(self._mapping)

    def find(self, city: str, state: str) -> tuple[float, float] | None:
        return self._mapping.get((normalize_city(city), (state or "").strip().upper()))

    @classmethod
    def load(
        cls,
        cache_dir: Path | None = None,
        *,
        with_geonames: bool = False,
    ) -> "CityLookup":
        cache_dir = Path(cache_dir) if cache_dir else default_cache_dir()

        mapping: dict[tuple[str, str], tuple[float, float]] = {}
        for key in ("place", "cousubs"):
            path = ensure_dataset(cache_dir, key)
            for (name, state), (_area, lat, lon) in _read_gazetteer(path).items():
                mapping.setdefault((name, state), (lat, lon))
            logger.info("Loaded %s (%s)", key, path.name)

        if with_geonames:
            path = ensure_dataset(cache_dir, "geonames")
            for (name, state), (_population, lat, lon) in _read_geonames(path).items():
                mapping.setdefault((name, state), (lat, lon))
            logger.info("Loaded geonames (%s)", path.name)

        return cls(mapping)
