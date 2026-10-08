# Fuel Route Planner

A Django + DRF API that plans a driving route between two locations in the
continental USA and computes the **cheapest possible set of fuel stops** along
the way, given the retail prices in `fuel-prices-for-be-assessment.csv`.

The vehicle model is fixed by the assessment:

* tank capacity: **50 gallons**
* fuel efficiency: **10 MPG** (maximum range: **500 miles**)

Routing and geocoding are provided by [OpenRouteService (HeiGIT)](https://openrouteservice.org/).
The API returns the route as GeoJSON, the optimal fuel stops, and the total
money spent on fuel. A Leaflet map page renders the same result.

---

## Features

* `POST /api/route/` – route + optimal fuel stops + total fuel cost (JSON).
* `GET /map/` – Leaflet map of the same route/stops (OpenStreetMap tiles
  served by the FOSSGIS mirror `tile.openstreetmap.de`, since the main
  volunteer server blocks embedded clients; swap the URL in
  `fuel/templates/fuel/map.html` for any other OSM provider), reusing the
  service layer and caches (no extra external API calls).
* `GET /health/` – liveness/readiness probe for Docker/Dokploy.
* Start and finish accept **addresses or `lat`/`lon` pairs**; both are
  validated to be inside the continental USA (lat 24–49, lon -125–-66).
* `start_full_tank` parameter (default `true`): with a full tank the initial
  50 gallons are **not** charged; with an empty tank the first purchase must
  happen at a station at/near the start location.
* Optimal fuel stops are chosen with a **dynamic program** that accounts for
  the price of fuel *and* the fuel burned on the detour to each station.
* Geocoding and route responses are cached (24 h / 1 h by default), so
  repeated requests and the map page never re-hit the external API.

---

## Project layout

```
fuel_route/                     Django project (settings, urls)
fuel/
  models.py                     FuelStation model
  serializers.py                Request validation for the API
  views.py                      RouteAPIView, MapView, health
  templates/fuel/map.html       Leaflet map page
  services/
    geo.py                      haversine / polyline helpers (numpy)
    ors.py                      OpenRouteService client (caching, retries)
    optimizer.py                candidate projection + DP fuel planner
    routing.py                  service layer shared by API and map
  management/commands/
    import_fuel_prices.py       CSV import + geocoding (resumable)
  tests/                        optimizer, ORS client, API, import tests
fuel-prices-for-be-assessment.csv
Dockerfile / docker-compose.yml / entrypoint.sh
postman_collection.json
```

---

## Quick start (docker-compose)

Requirements: Docker with the compose plugin.

```bash
cp .env.example .env
# edit .env and set OPENROUTESERVICE_API_KEY (and SECRET_KEY for production)

docker compose up --build
```

This starts:

* `db` – PostgreSQL 15 (exposed on host port **55432** to avoid clashing with
  a locally installed Postgres),
* `web` – Django dev server on <http://localhost:8000> (code is volume-mounted).

Then import the fuel prices (see below) and open
<http://localhost:8000/map/?start=Los+Angeles,+CA&finish=New+York,+NY>.

To run Django on the host instead of in Docker, use the same `.env` (the
`DATABASE_URL` in `.env.example` already points at `localhost:55432`) and:

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
python manage.py migrate
python manage.py runserver
```

---

## Importing fuel prices

```bash
python manage.py import_fuel_prices
```

What the command does:

1. Reads the CSV, strips whitespace, drops malformed rows.
2. Skips rows outside the USA (the CSV contains ~620 Canadian rows;
   use `--include-non-us` to keep them).
3. Deduplicates by `(truckstop_id, address, city, state)` and keeps the
   **minimum retail price** for each station (rows without a truckstop id are
   keyed by name + address + city + state).
4. Geocodes every unique station with the ORS geocoding API. The CSV
   addresses are often highway-exit descriptions (`"I-44, EXIT 283 & US-69"`),
   so when the full address cannot be resolved the command falls back to
   `city, state` and stores the station with `is_approximate=true`.
5. Saves/updates `FuelStation` rows (`update_or_create`), so the command is
   **idempotent and resumable** – already geocoded stations are skipped and a
   run that hit the daily quota can simply be repeated later.

Useful options:

```bash
python manage.py import_fuel_prices --limit 100          # first 100 unique stations
python manage.py import_fuel_prices --states CA,AZ,NM    # only some states
python manage.py import_fuel_prices --delay 1.0          # slower, safer
python manage.py import_fuel_prices --dry-run            # parse only
python manage.py import_fuel_prices --force              # re-geocode everything
```

### Validating stored coordinates

Highway-exit addresses can match the named highway in a different city or
state, so the importer constrains every query to the station's state bounding
box and only trusts an address result when it is within
`GEOCODE_CITY_MAX_DISTANCE_MILES` (default 15 mi) of the city centroid;
everything else falls back to the centroid with `is_approximate=true`.

To audit and repair an existing database:

```bash
python manage.py validate_stations                    # report suspicious rows
python manage.py validate_stations --full             # also check distance to the city centroid
python manage.py validate_stations --fix --city-only  # repair (quota-friendly)
```

The command is resumable and skips stations that already pass validation.

### Quota notes (important)

* A **pre-geocoded fixture** is included in the repository
  (`fuel/fixtures/fuel_stations.json`, ~2,250 stations covering the I-80
  corridor and more). Load it with `python manage.py loaddata fuel_stations`
  to populate a database without spending any geocoding quota – this is the
  recommended production path. Refresh the fixture locally after further
  import runs with:

  ```bash
  python manage.py dumpdata fuel.fuelstation > fuel/fixtures/fuel_stations.json
  ```

* The **current** HeiGIT API host `api.heigit.org` allows ~3000 geocoding
  requests/day (free "Standard" plan). The old host
  `api.openrouteservice.org` is deprecated and heavily throttled
  (100 geocoding requests/day) – this project uses `api.heigit.org`.
* The full dataset has ~6,600 US stations, so a complete import spans about
  **2–3 daily quota windows**. Just re-run the command after the quota resets;
  it continues where it stopped (the daily window resets 24 h after your first
  request).
* For a quick demo of the LA → New York route, import the I-80 corridor first
  (~2,100 stations, fits in one day):

  ```bash
  python manage.py import_fuel_prices --states CA,NV,UT,CO,NE,IA,IL,IN,OH,PA,NJ,NY,AZ,MI
  ```

---

## API

### `POST /api/route/`

Request:

```json
{
  "start": {"address": "Los Angeles, CA"},
  "finish": {"address": "New York, NY"},
  "start_full_tank": true
}
```

`start`/`finish` accept either `{"address": "..."}` **or**
`{"lat": 34.0522, "lon": -118.2437}`. `start_full_tank` is optional
(default `true`).

Response (abridged):

```json
{
  "route": {"type": "LineString", "coordinates": [[-118.24, 34.05], ...]},
  "total_distance_miles": 2790.9,
  "route_duration_hours": 41.2,
  "fuel_stops": [
    {
      "name": "PILOT TRAVEL CENTER #1243",
      "address": "I-8, EXIT 119 & SR-85",
      "city": "Gila Bend",
      "state": "AZ",
      "latitude": 32.9466,
      "longitude": -112.7188,
      "price": 3.899,
      "gallons_purchased": 24.5,
      "cost": 95.53,
      "distance_from_start_miles": 345.2,
      "detour_miles": 2.1,
      "is_approximate": false,
      "is_start_fuel": false
    }
  ],
  "total_fuel_cost": 312.45,
  "total_gallons_purchased": 96.5,
  "remaining_fuel_gallons": 3.0,
  "start": {"label": "Los Angeles, CA, USA", "latitude": 34.05, "longitude": -118.24},
  "finish": {"label": "New York, NY, USA", "latitude": 40.71, "longitude": -74.0},
  "start_full_tank": true,
  "map_url": "/map/?start=Los+Angeles%2C+CA&finish=New+York%2C+NY&start_full_tank=true"
}
```

Status codes:

| Status | Meaning |
| ------ | ------- |
| 200 | plan computed |
| 400 | invalid payload, unresolvable address, or location outside the USA |
| 422 | no feasible refuelling plan (e.g. a >500 mile stretch without stations) |
| 502 | ORS route/geocoding failure |
| 503 | `OPENROUTESERVICE_API_KEY` missing or rejected |

### `GET /map/`

Query string: `start`, `finish` (address **or** `lat,lon`),
`start_full_tank` (`true`/`false`). Defaults: Los Angeles → New York.

```
/map/?start=Los+Angeles,+CA&finish=New+York,+NY&start_full_tank=false
```

The view calls the same `build_route_response` service function as the API, so
the route and geocoding caches are shared and the external API is not called
twice.

### `GET /health/`

Returns `{"status": "ok", "database": true}`.

---

## Fuel optimisation algorithm

1. **Candidate selection**
   * Query stations inside the route's bounding box (indexed lat/lon columns).
   * Project every station onto the route polyline (vectorised numpy, local
     tangent-plane approximation) to get its `distance_from_start_miles`.
   * `detour_miles = 2 × distance to the closest point on the route`; stations
     with a detour above `FUEL_STATION_RADIUS_MILES` (default 10 mi) are
     discarded.
   * Sort the survivors by distance along the route.
2. **Dynamic program** – `dp[i][f]` = minimum cost to be at station `i` with
   `f` gallons in the tank. Fuel levels are discretised in 0.5-gallon steps
   (configurable via `FUEL_GRID_STEP_GALLONS`).
   * A purchase at station `i` costs
     `(net_gallons + detour_miles/10) × price_i`: the detour fuel is paid for
     at that station (assessment's detour model).
   * Purchases are optimised in closed form (`min_j dp[i][j] − f_j·price`),
     so each transition is linear in the number of fuel levels and a
     cross-country request with several hundred candidates solves in
     well under a second.
   * `start_full_tank=true` starts at 50 gallons; `false` starts at 0 and
     models a virtual stop at mile 0 whose price/detour come from the cheapest
     real station within the radius of the start point.
3. **Result** – minimum cost to reach the finish; the predecessor chain is
   walked backwards to produce the stop list. If no state reaches the finish,
   the API returns `422 no_feasible_fuel_plan`.

### Assumptions

* `gallons_purchased` is the **pump amount** (net fuel added to the tank plus
  the fuel burned on the detour), so `cost = gallons_purchased × price`.
* The initial 50 gallons of a full-tank start are free (not counted in
  `total_fuel_cost`), per the assessment.
* An empty-tank start uses the cheapest station within the radius of the start
  point as the first fuel stop (`is_start_fuel: true`).
* Highway-exit addresses that cannot be resolved as street addresses fall back
  to the city centroid (`is_approximate: true`).
* Route polylines are simplified by ≤ ~8 m for storage/display; this is far
  below the resolution of the station detour calculations.
* Distances along the route are computed with the haversine formula on the
  polyline (within ~0.5 % of the ORS summary distance).
* The US validation is the assessment's bounding box (24–49 / -125–-66), so a
  few locations in southern Canada fall inside it.

---

## Map providers

Routing and geocoding go through a small dispatcher (`fuel/services/maps.py`),
so the API, map page and optimizer are provider agnostic:

| Setting | Values | Default | Notes |
| ------- | ------ | ------- | ----- |
| `ROUTING_PROVIDER` | `ors`, `osrm`, `auto` | `ors` | `osrm` uses the keyless public OSRM demo server |
| `GEOCODING_PROVIDER` | `ors`, `nominatim`, `auto` | `ors` | `nominatim` uses the keyless public Nominatim service |

`auto` tries ORS first and transparently falls back to the free provider when
ORS is unavailable (quota exhausted, missing/rejected key, or repeated
upstream failures), logging a single warning. This is handy for demos: with
`GEOCODING_PROVIDER=auto`, address inputs keep working even after the ORS
geocoding quota is used up.

```bash
# .env – keyless routing + geocoding
ROUTING_PROVIDER=osrm
GEOCODING_PROVIDER=nominatim
```

Provider settings are read at startup, so restart the app after editing
`.env` (`docker compose restart web`).

**Usage policy caveats**

* The public OSRM demo server (`router.project-osrm.org`) and Nominatim
  (`nominatim.openstreetmap.org`) are intended for light/demo usage. Nominatim
  allows at most ~1 request/second and requires a descriptive User-Agent
  (`MAPS_USER_AGENT`); production deployments should self-host or use a paid
  provider.
* Bulk geocoding through the public Nominatim is not permitted, so
  `import_fuel_prices` always uses ORS unless you explicitly pass
  `--allow-public-nominatim`.
* OpenRouteService remains the default and the provider required by the
  assessment brief.

---

## Caching

| Data | Cache | TTL |
| ---- | ----- | --- |
| Geocoding results (including negative results) | Django cache | `GEOCODE_CACHE_TTL` (default 24 h) |
| Route (GeoJSON + summary) | Django cache | `ROUTE_CACHE_TTL` (default 1 h) |
| Geocoded stations | Database (`FuelStation`) | permanent |

Cache keys are namespaced per provider, so switching providers never serves
stale results from another one.

The default cache backend is Django's local-memory cache. In production set
`REDIS_URL` to use Redis (recommended when running multiple gunicorn workers).

---

## Tests

```bash
python manage.py test            # all 46 tests
python manage.py test fuel.tests.test_optimizer
```

* `test_optimizer.py` – polyline projection/detour, DP scenarios (full vs
  empty tank, single/multiple stations, detour trade-offs, infeasible gaps)
  and an independent brute-force cross-check.
* `test_ors.py` – geocoding/directions parsing, caching, retries, error mapping.
* `test_api.py` – API validation, response shape, 400/422/502/503 paths, map
  rendering (ORS mocked).
* `test_import_command.py` – dedupe/min-price, city fallback, idempotency.

The tests use the same Postgres as the app (Django creates a `test_*`
database) and never call the external API.

---

## Docker / Dokploy deployment

The `Dockerfile` builds a production image: Python 3.12-slim, gunicorn on
port 8000, `entrypoint.sh` waits for Postgres, runs `migrate` and
`collectstatic`, then starts gunicorn. A `/health/` healthcheck is included.

On [Dokploy](https://dokploy.com/):

1. Create a new application from your Git repository and choose the
   **Dockerfile** build type (Dockerfile path: `Dockerfile`).
2. Add a PostgreSQL database (or use an external one) and set:
   * `DATABASE_URL` – e.g. `postgres://user:pass@host:5432/fuel_route`
   * `SECRET_KEY` – long random string
   * `DEBUG=false`
   * `ALLOWED_HOSTS` – your domain(s), comma separated
   * `CSRF_TRUSTED_ORIGINS` – e.g. `https://your-domain`
   * `OPENROUTESERVICE_API_KEY` – your key
   * optionally `REDIS_URL`, `ROUTE_CACHE_TTL`, `GEOCODE_CACHE_TTL`
3. Expose container port **8000** (Dokploy maps your domain to it).
4. Deploy. Then load the pre-geocoded station fixture once (Dokploy terminal or
   `docker exec`):

   ```bash
   python manage.py loaddata fuel_stations
   ```

   The fixture ships with the repository (`fuel/fixtures/fuel_stations.json`,
   generated from this project's own import run), so production **never calls
   the geocoder** for those stations and your ORS quota stays untouched. After
   that, `python manage.py import_fuel_prices` only processes states that are
   not covered yet (existing stations are skipped – no quota spent).

   To import everything from scratch instead (uses your geocoding quota,
   ~2–3 daily windows):

   ```bash
   python manage.py import_fuel_prices
   ```

Notes:

* The image does not include a Redis server; either point `REDIS_URL` at a
  managed Redis or keep the local-memory cache (fine for a single worker).
* The CSV is part of the image, so the import works out of the box.
* `api.heigit.org` is the current ORS API host; `api.openrouteservice.org` is
  deprecated and rate-limited.

---

## Troubleshooting

| Symptom | Fix |
| ------- | --- |
| `OPENROUTESERVICE_API_KEY is not configured` (503) | set the env var / `.env` |
| `ORS daily quota reached` during import | wait for the reset window and re-run the command (progress is kept) |
| 422 `no_feasible_fuel_plan` | no stations within 10 mi of the route for a >500 mi stretch – import more states |
| `Could not geocode address` (400) | use a more specific address or `lat`/`lon` |
| Map page shows an error banner | the API response contains the same error message and code |
