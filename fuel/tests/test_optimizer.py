"""Unit tests for the fuel stop optimiser (projection + dynamic program)."""

from __future__ import annotations

import random
from decimal import Decimal
from types import SimpleNamespace

import numpy as np
from django.test import TestCase, override_settings

from fuel.services import geo
from fuel.services.optimizer import (
    Candidate,
    NoFeasiblePlanError,
    optimize_fuel_stops,
    route_length_miles,
    select_candidates,
)

MPG = 10.0
CAP = 50.0


def make_candidate(along, price, detour=0.0, name="Station", latitude=35.0, longitude=-100.0):
    return Candidate(
        station_id=None,
        name=name,
        address="1 Main St",
        city="Somewhere",
        state="TX",
        latitude=latitude,
        longitude=longitude,
        price=float(price),
        is_approximate=False,
        along_miles=float(along),
        detour_miles=float(detour),
    )


def make_station(latitude, longitude, price="3.000", name="Station"):
    return SimpleNamespace(
        id=None,
        name=name,
        address="1 Main St",
        city="Somewhere",
        state="TX",
        latitude=latitude,
        longitude=longitude,
        price=Decimal(price),
        is_approximate=False,
    )


def straight_route(miles: float, points: int = 200) -> np.ndarray:
    """Straight south->north route of ``miles`` miles at longitude -100."""
    lat0 = 30.0
    lat1 = lat0 + miles / geo.MILES_PER_DEGREE_LAT
    return np.column_stack([np.linspace(lat0, lat1, points), np.full(points, -100.0)])


def brute_force_cost(route_miles, candidates, mpg=MPG, capacity=CAP):
    """Independent exhaustive solver (integer gallons, 10-mile legs).

    Distances and detours must be multiples of 10 miles so an integer-gallon
    optimum exists and the result is comparable with the DP.
    """
    n = len(candidates)
    positions = [0.0] + [c.along_miles for c in candidates] + [route_miles]
    detours = [0.0] + [c.detour_miles for c in candidates] + [0.0]
    prices = [0.0] + [c.price for c in candidates] + [0.0]
    infinity = float("inf")
    best_to_finish: dict[tuple[int, int], float] = {}

    def solve(i: int, fuel: int) -> float:
        key = (i, fuel)
        if key in best_to_finish:
            return best_to_finish[key]
        best = infinity
        for j in range(i + 1, n + 2):
            distance = positions[j] - positions[i]
            if distance > fuel * mpg:
                continue
            if j == n + 1:
                best = min(best, 0.0)
                continue
            arrival = fuel - distance / mpg
            if abs(arrival - round(arrival)) > 1e-9:
                continue
            arrival = int(round(arrival))
            for departure in range(arrival, int(capacity) + 1):
                extra = (departure - arrival + detours[j] / mpg) * prices[j]
                tail = solve(j, departure)
                if tail < infinity:
                    best = min(best, extra + tail)
        best_to_finish[key] = best
        return best

    return solve(0, int(capacity))


class ProjectionTests(TestCase):
    def test_station_on_route_has_zero_detour(self):
        coords = straight_route(100)
        station = make_station(30.0, -100.0)
        candidates = select_candidates(coords, [station], radius_miles=10)
        self.assertEqual(len(candidates), 1)
        self.assertAlmostEqual(candidates[0].detour_miles, 0.0, places=6)
        self.assertAlmostEqual(candidates[0].along_miles, 0.0, places=3)

    def test_station_offset_within_radius_kept_with_detour(self):
        coords = straight_route(100)
        offset_miles = 4.0
        station_lat = 30.0 + 50.0 / geo.MILES_PER_DEGREE_LAT
        station_lon = -100.0 + offset_miles / (
            geo.MILES_PER_DEGREE_LAT * np.cos(np.radians(station_lat))
        )
        station = make_station(station_lat, station_lon)
        candidates = select_candidates(coords, [station], radius_miles=10)
        self.assertEqual(len(candidates), 1)
        self.assertAlmostEqual(candidates[0].detour_miles, 2 * offset_miles, delta=0.05)
        self.assertAlmostEqual(candidates[0].along_miles, 50.0, delta=0.5)

    def test_station_outside_radius_excluded(self):
        coords = straight_route(100)
        offset_miles = 7.0  # detour of 14 miles > radius
        station_lat = 30.0 + 50.0 / geo.MILES_PER_DEGREE_LAT
        station_lon = -100.0 + offset_miles / (
            geo.MILES_PER_DEGREE_LAT * np.cos(np.radians(station_lat))
        )
        candidates = select_candidates(coords, [make_station(station_lat, station_lon)], radius_miles=10)
        self.assertEqual(candidates, [])

    def test_candidates_sorted_by_distance_along_route(self):
        coords = straight_route(100)
        lat = lambda miles: 30.0 + miles / geo.MILES_PER_DEGREE_LAT
        stations = [make_station(lat(80), -100.0), make_station(lat(20), -100.0)]
        candidates = select_candidates(coords, stations, radius_miles=10)
        self.assertEqual([round(c.along_miles) for c in candidates], [20, 80])

    def test_route_length(self):
        coords = straight_route(123.4)
        self.assertAlmostEqual(route_length_miles(coords), 123.4, delta=0.2)


@override_settings(ORS_REQUEST_INTERVAL=0.0)
class OptimizerTests(TestCase):
    def test_full_tank_short_trip_needs_no_stops(self):
        plan = optimize_fuel_stops(400.0, [], start_full_tank=True)
        self.assertEqual(plan.stops, [])
        self.assertEqual(plan.total_cost, 0.0)

    def test_single_station_full_tank(self):
        # 900 miles, full tank (500 range): must buy 40 gallons at mile 400.
        candidates = [make_candidate(400.0, 3.0)]
        plan = optimize_fuel_stops(900.0, candidates, start_full_tank=True)
        self.assertEqual(len(plan.stops), 1)
        self.assertAlmostEqual(plan.stops[0].gallons_purchased, 40.0, places=6)
        self.assertAlmostEqual(plan.total_cost, 120.0, places=6)

    def test_empty_tank_must_buy_at_start(self):
        # Route 900 with a station at the start (mile 0) and one at mile 400.
        candidates = [make_candidate(0.0, 3.0), make_candidate(400.0, 3.0)]
        plan = optimize_fuel_stops(
            900.0, candidates, start_full_tank=False, start_coordinates=(35.0, -100.0)
        )
        self.assertTrue(plan.stops[0].is_start_fuel)
        self.assertAlmostEqual(plan.total_gallons, 90.0, places=6)
        self.assertAlmostEqual(plan.total_cost, 270.0, places=6)

    def test_empty_tank_requires_station_near_start(self):
        # Station 100 miles away is not reachable with an empty tank.
        candidates = [make_candidate(100.0, 3.0, latitude=31.0, longitude=-100.0)]
        with self.assertRaises(NoFeasiblePlanError):
            optimize_fuel_stops(
                600.0,
                candidates,
                start_full_tank=False,
                start_coordinates=(30.0, -100.0),
            )

    def test_prefers_cheaper_station_ahead(self):
        # Full tank, 1200 miles: A@300 $4.00, B@700 $2.00 -> buy 20 gal at A
        # and 50 gal at B (total 180).
        candidates = [make_candidate(300.0, 4.0, name="A"), make_candidate(700.0, 2.0, name="B")]
        plan = optimize_fuel_stops(1200.0, candidates, start_full_tank=True)
        self.assertAlmostEqual(plan.total_cost, 180.0, places=6)
        names = [stop.name for stop in plan.stops]
        self.assertEqual(names, ["A", "B"])
        self.assertAlmostEqual(plan.stops[0].gallons_purchased, 20.0, places=6)

    def test_detour_cost_can_flip_the_choice(self):
        # Same position and (nearly) same price: the station with the detour
        # loses once its price advantage is smaller than the detour fuel cost.
        cheap_but_far = make_candidate(400.0, 2.19, detour=10.0, name="Far")
        near = make_candidate(400.0, 2.20, detour=0.0, name="Near")
        plan = optimize_fuel_stops(900.0, [cheap_but_far, near], start_full_tank=True)
        self.assertEqual([stop.name for stop in plan.stops], ["Near"])
        self.assertAlmostEqual(plan.total_cost, 40.0 * 2.20, places=6)

        # Without the detour the cheaper station wins.
        cheap_on_route = make_candidate(400.0, 2.19, detour=0.0, name="Cheap")
        plan = optimize_fuel_stops(900.0, [cheap_on_route, near], start_full_tank=True)
        self.assertEqual([stop.name for stop in plan.stops], ["Cheap"])
        self.assertAlmostEqual(plan.total_cost, 40.0 * 2.19, places=6)

    def test_detour_fuel_is_charged_at_the_station(self):
        # 900 miles, one station at mile 400 with a 10 mile detour -> 41 gallons.
        candidates = [make_candidate(400.0, 3.0, detour=10.0)]
        plan = optimize_fuel_stops(900.0, candidates, start_full_tank=True)
        self.assertAlmostEqual(plan.stops[0].gallons_purchased, 41.0, places=6)
        self.assertAlmostEqual(plan.stops[0].cost, 123.0, places=6)
        self.assertAlmostEqual(plan.total_cost, 123.0, places=6)

    def test_infeasible_gap_raises(self):
        # After mile 300 there is no station for the remaining 900 miles.
        candidates = [make_candidate(300.0, 3.0)]
        with self.assertRaises(NoFeasiblePlanError):
            optimize_fuel_stops(1200.0, candidates, start_full_tank=True)

    def test_infeasible_with_no_stations(self):
        with self.assertRaises(NoFeasiblePlanError):
            optimize_fuel_stops(600.0, [], start_full_tank=True)

    def test_plan_is_physically_consistent(self):
        candidates = [
            make_candidate(200.0, 3.5, detour=4.0),
            make_candidate(450.0, 2.9, detour=0.0),
            make_candidate(700.0, 3.1, detour=6.0),
            make_candidate(1000.0, 2.7, detour=2.0),
        ]
        route_miles = 1400.0
        plan = optimize_fuel_stops(route_miles, candidates, start_full_tank=True)

        fuel = CAP
        previous = 0.0
        for stop in plan.stops:
            driven = stop.distance_from_start_miles - previous
            self.assertGreaterEqual(fuel * MPG + 1e-6, driven)
            fuel -= driven / MPG
            fuel += stop.gallons_purchased - stop.detour_miles / MPG
            self.assertLessEqual(fuel, CAP + 1e-6)
            previous = stop.distance_from_start_miles
        driven = route_miles - previous
        self.assertGreaterEqual(fuel * MPG + 1e-6, driven)

    def test_matches_brute_force_reference_solution(self):
        scenarios = [
            # (route miles, [(along, price, detour), ...])
            (900.0, [(300.0, 3.9, 0.0), (600.0, 3.2, 0.0)]),
            (1200.0, [(300.0, 4.1, 0.0), (700.0, 2.8, 10.0), (1000.0, 3.4, 0.0)]),
            (1400.0, [(250.0, 3.3, 10.0), (500.0, 3.9, 0.0), (900.0, 2.9, 0.0), (1150.0, 4.2, 0.0)]),
        ]
        rng = random.Random(7)
        for _ in range(4):
            route = float(rng.choice([900, 1000, 1100, 1300]))
            stations = []
            along = 100.0
            for _ in range(rng.randint(2, 4)):
                along += rng.choice([100.0, 150.0, 200.0])
                if along >= route:
                    break
                stations.append((along, round(rng.uniform(2.6, 4.4), 1), rng.choice([0.0, 10.0])))
            if not stations:
                continue
            # Guarantee feasibility: the last stop must be within range of the finish.
            if route - stations[-1][0] > 450:
                stations.append((route - 300.0, round(rng.uniform(2.6, 4.4), 1), 0.0))
            scenarios.append((route, stations))

        for route_miles, station_specs in scenarios:
            candidates = [make_candidate(a, p, d) for a, p, d in station_specs]
            expected = brute_force_cost(route_miles, candidates)
            plan = optimize_fuel_stops(route_miles, candidates, start_full_tank=True)
            self.assertAlmostEqual(
                plan.total_cost,
                expected,
                places=6,
                msg=f"scenario {station_specs} on {route_miles} miles",
            )
