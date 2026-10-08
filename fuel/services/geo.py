"""Pure geo helpers used by the fuel optimisation (no Django imports)."""

from __future__ import annotations

import math

import numpy as np

EARTH_RADIUS_MILES = 3958.7613
MILES_PER_DEGREE_LAT = 69.172


def haversine_miles(lat1, lon1, lat2, lon2):
    """Great-circle distance in miles. Works with scalars and numpy arrays."""
    lat1 = np.radians(np.asarray(lat1, dtype=float))
    lon1 = np.radians(np.asarray(lon1, dtype=float))
    lat2 = np.radians(np.asarray(lat2, dtype=float))
    lon2 = np.radians(np.asarray(lon2, dtype=float))
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    a = np.sin(dlat / 2.0) ** 2 + np.cos(lat1) * np.cos(lat2) * np.sin(dlon / 2.0) ** 2
    c = 2.0 * np.arcsin(np.sqrt(np.clip(a, 0.0, 1.0)))
    result = EARTH_RADIUS_MILES * c
    return float(result) if np.isscalar(result) else result


def cumulative_miles(coords: np.ndarray) -> np.ndarray:
    """Cumulative distance along a ``(N, 2)`` array of ``(lat, lon)`` points."""
    coords = np.asarray(coords, dtype=float)
    if len(coords) == 0:
        return np.zeros(0)
    if len(coords) == 1:
        return np.zeros(1)
    segment = haversine_miles(
        coords[:-1, 0], coords[:-1, 1], coords[1:, 0], coords[1:, 1]
    )
    return np.concatenate([[0.0], np.cumsum(segment)])


def decode_polyline(encoded: str, precision: int = 5) -> list[tuple[float, float]]:
    """Decode a Google/ORS/OSRM encoded polyline into ``(lat, lon)`` tuples.

    Both routing providers can return geometry in this compact format (ORS
    ``/json``, OSRM ``geometries=polyline``), which is several times smaller
    than GeoJSON on the wire. ``precision`` 5 (the default) is ~1 m – far below
    the tolerance used when simplifying the route for storage/display.
    """
    coordinates: list[tuple[float, float]] = []
    index = 0
    lat = 0
    lon = 0
    length = len(encoded)
    while index < length:
        values = [0, 0]
        for axis in (0, 1):
            result = 0
            shift = 0
            while True:
                chunk = ord(encoded[index]) - 63
                index += 1
                result |= (chunk & 0x1F) << shift
                shift += 5
                if chunk < 0x20:
                    break
            values[axis] = ~(result >> 1) if result & 1 else result >> 1
        lat += values[0]
        lon += values[1]
        coordinates.append((lat / 10**precision, lon / 10**precision))
    return coordinates


def simplify_polyline(coords: np.ndarray, tolerance_miles: float = 0.002) -> np.ndarray:
    """Douglas-Peucker simplification using a local equirectangular plane."""
    coords = np.asarray(coords, dtype=float)
    n = len(coords)
    if n <= 2:
        return coords

    lat_ref = float(np.mean(coords[:, 0]))
    scale_x = MILES_PER_DEGREE_LAT * math.cos(math.radians(lat_ref))
    scale_y = MILES_PER_DEGREE_LAT
    xy = np.column_stack((coords[:, 1] * scale_x, coords[:, 0] * scale_y))

    keep = np.zeros(n, dtype=bool)
    keep[0] = keep[-1] = True
    stack = [(0, n - 1)]
    while stack:
        i, j = stack.pop()
        if j <= i + 1:
            continue
        a = xy[i]
        b = xy[j]
        ab = b - a
        length_sq = float(ab @ ab)
        middle = xy[i + 1 : j]
        if length_sq < 1e-12:
            distances = np.hypot(middle[:, 0] - a[0], middle[:, 1] - a[1])
        else:
            t = np.clip(((middle - a) @ ab) / length_sq, 0.0, 1.0)
            proj = a + t[:, None] * ab
            distances = np.hypot(middle[:, 0] - proj[:, 0], middle[:, 1] - proj[:, 1])
        k = int(np.argmax(distances))
        if distances[k] > tolerance_miles:
            m = i + 1 + k
            keep[m] = True
            stack.append((i, m))
            stack.append((m, j))
    return coords[keep]
