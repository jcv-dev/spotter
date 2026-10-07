"""Optimal fuel-stop selection.

Stage 1 – candidate selection: project every station near the route onto the
route polyline (numpy) and compute its distance from the start and the detour
distance (2 x distance to the route, as per the assessment).

Stage 2 – dynamic program: discretise the fuel level (0.5 gallon steps by
default) and minimise the total fuel purchased, where the fuel burned during
a detour must be replenished at that station and is therefore paid for there.

The DP has states ``dp[i][f]`` = minimum cost to be at candidate station ``i``
with ``f`` gallons in the tank.  Purchases are optimised in closed form
(``min_j dp[i][j] - f_j * price``), which keeps the transition step linear in
the number of fuel levels.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass

import numpy as np
from django.conf import settings

from .geo import MILES_PER_DEGREE_LAT, cumulative_miles, haversine_miles

logger = logging.getLogger("fuel.optimizer")


class NoFeasiblePlanError(Exception):
    """Raised when the destination cannot be reached with the fuel stops available."""


@dataclass(frozen=True)
class Candidate:
    """A fuel station projected onto the route."""

    station_id: int | None
    name: str
    address: str
    city: str
    state: str
    latitude: float
    longitude: float
    price: float
    is_approximate: bool
    along_miles: float
    detour_miles: float


@dataclass(frozen=True)
class FuelStop:
    name: str
    address: str
    city: str
    state: str
    latitude: float
    longitude: float
    price: float
    gallons_purchased: float
    cost: float
    distance_from_start_miles: float
    detour_miles: float
    is_approximate: bool = False
    is_start_fuel: bool = False


@dataclass
class FuelPlan:
    stops: list[FuelStop]
    total_cost: float
    total_gallons: float
    remaining_fuel_gallons: float


def route_length_miles(coords: np.ndarray) -> float:
    cum = cumulative_miles(coords)
    return float(cum[-1]) if len(cum) else 0.0


def select_candidates(
    coords: np.ndarray,
    stations,
    radius_miles: float,
    *,
    window: int = 80,
) -> list[Candidate]:
    """Project stations onto the route and keep those within ``radius_miles``."""
    coords = np.asarray(coords, dtype=float)
    if len(coords) < 2:
        return []

    cum = cumulative_miles(coords)
    lat_ref = float(coords[0, 0])
    lon_ref = float(coords[0, 1])
    cos_ref = max(math.cos(math.radians(lat_ref)), 1e-6)

    # Rough planar coordinates, only used to locate the nearest vertex.
    plane_x = (coords[:, 1] - lon_ref) * MILES_PER_DEGREE_LAT * cos_ref
    plane_y = (coords[:, 0] - lat_ref) * MILES_PER_DEGREE_LAT

    candidates: list[Candidate] = []
    for station in stations:
        s_lat = float(station.latitude)
        s_lon = float(station.longitude)
        st_x = (s_lon - lon_ref) * MILES_PER_DEGREE_LAT * cos_ref
        st_y = (s_lat - lat_ref) * MILES_PER_DEGREE_LAT

        nearest = int(np.argmin((plane_x - st_x) ** 2 + (plane_y - st_y) ** 2))
        lo = max(0, nearest - window)
        hi = min(len(coords) - 1, nearest + window)
        if hi <= lo:
            continue

        # Exact-ish projection in the station's local tangent plane.
        cos_st = max(math.cos(math.radians(s_lat)), 1e-6)
        ax = (coords[lo:hi, 1] - s_lon) * MILES_PER_DEGREE_LAT * cos_st
        ay = (coords[lo:hi, 0] - s_lat) * MILES_PER_DEGREE_LAT
        bx = (coords[lo + 1 : hi + 1, 1] - s_lon) * MILES_PER_DEGREE_LAT * cos_st
        by = (coords[lo + 1 : hi + 1, 0] - s_lat) * MILES_PER_DEGREE_LAT
        abx = bx - ax
        aby = by - ay
        seg_len_sq = abx * abx + aby * aby
        seg_len_sq[seg_len_sq == 0] = 1e-12

        # P is the origin; the projection parameter is dot(-A, AB) / |AB|^2.
        t = np.clip(-(ax * abx + ay * aby) / seg_len_sq, 0.0, 1.0)
        cx = ax + t * abx
        cy = ay + t * aby
        distances = np.hypot(cx, cy)

        k = int(np.argmin(distances))
        distance_to_route = float(distances[k])
        if distance_to_route > radius_miles:
            continue

        segment = lo + k
        along = float(cum[segment] + t[k] * (cum[segment + 1] - cum[segment]))
        along = min(max(along, 0.0), float(cum[-1]))

        candidates.append(
            Candidate(
                station_id=getattr(station, "id", None),
                name=station.name,
                address=station.address,
                city=station.city,
                state=station.state,
                latitude=s_lat,
                longitude=s_lon,
                price=float(station.price),
                is_approximate=bool(getattr(station, "is_approximate", False)),
                along_miles=along,
                detour_miles=2.0 * distance_to_route,
            )
        )

    candidates.sort(key=lambda c: (c.along_miles, c.detour_miles, c.price))
    logger.info(
        "Selected %d candidate stations within %g miles of the route", len(candidates), radius_miles
    )
    return candidates


def _station_near_start(
    candidates: list[Candidate], start_coordinates: tuple[float, float], radius_miles: float
) -> Candidate | None:
    """Cheapest station reachable from the start point (used for empty-tank starts)."""
    start_lat, start_lon = start_coordinates
    best: Candidate | None = None
    best_key: tuple[float, float] | None = None
    for candidate in candidates:
        distance = float(
            haversine_miles(start_lat, start_lon, candidate.latitude, candidate.longitude)
        )
        if distance > radius_miles:
            continue
        key = (candidate.price, distance)
        if best_key is None or key < best_key:
            best = candidate
            best_key = key
    return best


def optimize_fuel_stops(
    route_miles: float,
    candidates: list[Candidate],
    *,
    start_full_tank: bool = True,
    start_coordinates: tuple[float, float] | None = None,
) -> FuelPlan:
    """Run the DP and return the cheapest refuelling plan.

    Raises :class:`NoFeasiblePlanError` when the destination cannot be reached.
    """
    tank_gallons = float(settings.TANK_CAPACITY_GALLONS)
    mpg = float(settings.MILES_PER_GALLON)
    step = float(settings.FUEL_GRID_STEP_GALLONS)
    max_range = tank_gallons * mpg
    radius_miles = float(settings.FUEL_STATION_RADIUS_MILES)

    grid = np.round(np.arange(0.0, tank_gallons + step / 2, step), 6)
    levels = len(grid)
    last = levels - 1
    inf = np.inf

    n = len(candidates)
    finish = n + 1

    positions = np.zeros(n + 2)
    if n:
        positions[1 : n + 1] = [c.along_miles for c in candidates]
    positions[finish] = route_miles

    dp = np.full((n + 2, levels), inf)
    prev_station = np.full((n + 2, levels), -1, dtype=np.int32)
    prev_level = np.full((n + 2, levels), -1, dtype=np.int32)
    prev_arrival = np.zeros((n + 2, levels), dtype=np.int32)
    departure_levels = np.arange(levels)

    start_station: Candidate | None = None
    if start_full_tank:
        dp[0, last] = 0.0
    else:
        if start_coordinates is None:
            raise ValueError("start_coordinates are required when start_full_tank is False")
        start_station = _station_near_start(candidates, start_coordinates, radius_miles)
        if start_station is None:
            raise NoFeasiblePlanError(
                f"No fuel station within {radius_miles:g} miles of the start location "
                "– cannot start the trip with an empty tank."
            )

    for i in range(0, n + 1):
        # ---- departure states at station i (after an optimal purchase) ----
        if i == 0:
            if start_full_tank:
                if not np.isfinite(dp[0]).any():
                    continue
                departure = dp[0].copy()
            else:
                assert start_station is not None
                price = start_station.price
                detour_gallons = start_station.detour_miles / mpg
                departure = (grid + detour_gallons) * price
                prev_arrival[0, :] = 0
        else:
            candidate = candidates[i - 1]
            if not np.isfinite(dp[i]).any():
                continue
            price = candidate.price
            detour_gallons = candidate.detour_miles / mpg
            # prefix minima of dp - level * price, with argmin bookkeeping
            adjusted = dp[i] - grid * price
            prefix = np.minimum.accumulate(adjusted)
            if not np.isfinite(prefix).any():
                continue
            best = inf
            best_index = 0
            for k in range(levels):
                value = adjusted[k]
                if value < best:
                    best = value
                    best_index = k
                prev_arrival[i, k] = best_index
            departure = grid * price + detour_gallons * price + prefix

        # ---- travel to every station (and the finish) within range ----
        j_max = int(np.searchsorted(positions, positions[i] + max_range, side="right") - 1)
        if j_max <= i:
            continue
        j_indices = np.arange(i + 1, j_max + 1)
        distances = positions[j_indices] - positions[i]
        needed = np.ceil(distances / (mpg * step) - 1e-9).astype(np.int64)
        reachable = needed <= last
        if not reachable.any():
            continue
        j_indices = j_indices[reachable]
        needed = needed[reachable]

        # arrival level k - needed(k); snapping down prevents overestimating fuel
        departure_index = needed[:, None] + departure_levels[None, :]
        valid = departure_index <= last
        safe_index = np.where(valid, departure_index, 0)
        costs = np.where(valid, departure[safe_index], inf)

        better = costs < dp[j_indices]
        if better.any():
            rows, cols = np.nonzero(better)
            targets = j_indices[rows]
            taken = departure_index[rows, cols]
            dp[targets, cols] = costs[rows, cols]
            prev_station[targets, cols] = i
            prev_level[targets, cols] = taken

    if not np.isfinite(dp[finish]).any():
        raise NoFeasiblePlanError(
            "No feasible refuelling plan: a stretch longer than "
            f"{max_range:g} miles has no station within {radius_miles:g} miles of the route."
        )

    final_level = int(np.argmin(dp[finish]))
    total_cost = float(dp[finish, final_level])

    # ---- reconstruct the stops (walking backwards) ----
    stops: list[FuelStop] = []
    row, level = finish, final_level
    while row > 0:
        source = int(prev_station[row, level])
        taken = int(prev_level[row, level])
        if source < 0:
            raise RuntimeError("Broken predecessor chain while reconstructing fuel plan")
        if source == 0:
            if not start_full_tank and start_station is not None:
                net_gallons = float(grid[taken])
                detour_gallons = start_station.detour_miles / mpg
                gallons = net_gallons + detour_gallons
                stops.append(
                    FuelStop(
                        name=start_station.name,
                        address=start_station.address,
                        city=start_station.city,
                        state=start_station.state,
                        latitude=start_station.latitude,
                        longitude=start_station.longitude,
                        price=start_station.price,
                        gallons_purchased=gallons,
                        cost=gallons * start_station.price,
                        distance_from_start_miles=start_station.along_miles,
                        detour_miles=start_station.detour_miles,
                        is_approximate=start_station.is_approximate,
                        is_start_fuel=True,
                    )
                )
            row = 0
        else:
            candidate = candidates[source - 1]
            arrival_level = int(prev_arrival[source, taken])
            net_gallons = float(grid[taken] - grid[arrival_level])
            if net_gallons > 1e-9:
                detour_gallons = candidate.detour_miles / mpg
                gallons = net_gallons + detour_gallons
                stops.append(
                    FuelStop(
                        name=candidate.name,
                        address=candidate.address,
                        city=candidate.city,
                        state=candidate.state,
                        latitude=candidate.latitude,
                        longitude=candidate.longitude,
                        price=candidate.price,
                        gallons_purchased=gallons,
                        cost=gallons * candidate.price,
                        distance_from_start_miles=candidate.along_miles,
                        detour_miles=candidate.detour_miles,
                        is_approximate=candidate.is_approximate,
                    )
                )
            row, level = source, arrival_level

    stops.reverse()
    return FuelPlan(
        stops=stops,
        total_cost=total_cost,
        total_gallons=sum(stop.gallons_purchased for stop in stops),
        remaining_fuel_gallons=float(grid[final_level]),
    )
