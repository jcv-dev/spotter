"""Approximate US state bounding boxes.

Used to constrain OpenRouteService geocoding queries to the right state and to
validate results afterwards. The boxes are intentionally a little generous
(they include a few miles beyond the state line) because the CSV contains
border towns.

Format: ``(min_lat, min_lon, max_lat, max_lon)``.
"""

from __future__ import annotations

STATE_BOUNDING_BOXES: dict[str, tuple[float, float, float, float]] = {
    "AL": (30.1, -88.5, 35.1, -84.8),
    "AK": (51.0, -179.0, 71.5, -129.0),
    "AZ": (31.2, -114.9, 37.1, -108.9),
    "AR": (33.0, -94.7, 36.6, -89.6),
    "CA": (32.4, -124.5, 42.1, -114.1),
    "CO": (36.9, -109.1, 41.1, -102.0),
    "CT": (40.9, -73.8, 42.1, -71.7),
    "DE": (38.4, -75.8, 39.9, -75.0),
    "DC": (38.7, -77.2, 39.0, -76.8),
    "FL": (24.3, -87.7, 31.1, -79.9),
    "GA": (30.3, -85.7, 35.1, -80.7),
    "HI": (18.8, -160.3, 22.3, -154.7),
    "ID": (41.9, -117.3, 49.1, -111.0),
    "IL": (36.9, -91.6, 42.6, -87.0),
    "IN": (37.7, -88.1, 41.8, -84.7),
    "IA": (40.3, -96.7, 43.6, -90.1),
    "KS": (36.9, -102.1, 40.1, -94.5),
    "KY": (36.4, -89.6, 39.2, -81.9),
    "LA": (28.9, -94.1, 33.1, -88.8),
    "ME": (42.9, -71.1, 47.5, -66.9),
    "MD": (37.8, -79.5, 39.8, -74.9),
    "MA": (41.1, -73.6, 42.9, -69.8),
    "MI": (41.6, -90.5, 48.3, -82.3),
    "MN": (43.4, -97.3, 49.4, -89.4),
    "MS": (30.1, -91.7, 35.1, -88.0),
    "MO": (36.0, -95.8, 40.7, -89.0),
    "MT": (44.3, -116.1, 49.1, -104.0),
    "NE": (39.9, -104.1, 43.1, -95.2),
    "NV": (35.0, -120.1, 42.1, -114.0),
    "NH": (42.6, -72.6, 45.4, -70.6),
    "NJ": (38.9, -75.6, 41.4, -73.8),
    "NM": (31.2, -109.1, 37.1, -102.9),
    "NY": (40.4, -79.9, 45.1, -71.7),
    "NC": (33.8, -84.4, 36.7, -75.4),
    "ND": (45.8, -104.1, 49.1, -96.5),
    "OH": (38.3, -84.9, 42.0, -80.4),
    "OK": (33.6, -103.1, 37.1, -94.4),
    "OR": (41.9, -124.7, 46.4, -116.4),
    "PA": (39.7, -80.6, 42.4, -74.6),
    "RI": (41.1, -71.9, 42.1, -71.1),
    "SC": (32.0, -83.4, 35.3, -78.5),
    "SD": (42.4, -104.1, 45.9, -96.4),
    "TN": (34.9, -90.4, 36.7, -81.6),
    "TX": (25.8, -106.7, 36.6, -93.5),
    "UT": (36.9, -114.1, 42.1, -108.9),
    "VT": (42.7, -73.5, 45.1, -71.4),
    "VA": (36.5, -83.7, 39.5, -75.1),
    "WA": (45.5, -124.8, 49.1, -116.9),
    "WV": (37.1, -82.7, 40.7, -77.7),
    "WI": (42.4, -92.9, 47.1, -86.7),
    "WY": (40.9, -111.1, 45.1, -103.9),
}


def bounding_box(state: str) -> tuple[float, float, float, float] | None:
    """Return ``(min_lat, min_lon, max_lat, max_lon)`` for a US state code."""
    return STATE_BOUNDING_BOXES.get((state or "").strip().upper())


def point_in_state(latitude: float, longitude: float, state: str, margin_degrees: float = 0.0) -> bool:
    """Whether a point is inside the state's approximate bounding box."""
    box = bounding_box(state)
    if box is None:
        return False
    min_lat, min_lon, max_lat, max_lon = box
    return (
        min_lat - margin_degrees <= latitude <= max_lat + margin_degrees
        and min_lon - margin_degrees <= longitude <= max_lon + margin_degrees
    )
