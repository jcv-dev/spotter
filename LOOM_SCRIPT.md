# Loom demo script (≈5 minutes)

A detailed run-through for recording the project demo. Times are suggestions;
the total should stay around five minutes.

## Before recording

1. Start the stack and import data (one-time):

   ```bash
   cp .env.example .env        # set OPENROUTESERVICE_API_KEY
   docker compose up --build -d
   docker compose exec web python manage.py import_fuel_prices --states CA,NV,UT,CO,NE,IA,IL,IN,OH,PA,NJ,NY,AZ,MI
   ```

   (This corridor fits in one daily geocoding quota window. Re-run the command
   later to import the remaining states – it is resumable.)

2. Open these tabs:
   * Terminal in the project root.
   * `http://localhost:8000/map/?start=Los+Angeles,+CA&finish=New+York,+NY`
   * Postman with the provided `postman_collection.json` imported
     (collection variable `base_url = http://localhost:8000`).
3. Optionally clear the cache before the demo so the first request shows the
   ORS call in the logs, then note how the second one is instant:
   `docker compose exec web python manage.py shell -c "from django.core.cache import cache; cache.clear()"`.

---

## 0:00 – 0:40 · Introduction

* "This is a Django + DRF API that plans a route between two locations in the
  USA and finds the cheapest fuel stops along the way."
* Vehicle model: 50-gallon tank, 10 MPG, 500-mile range.
* Data: ~8,100 fuel-price rows from the assessment CSV, geocoded with
  OpenRouteService; routing also via OpenRouteService.
* Two main endpoints: `POST /api/route/` (JSON) and `GET /map/` (Leaflet map),
  plus a `/health/` probe.

## 0:40 – 1:30 · Code structure

Show the tree and highlight:

* `fuel/models.py` – `FuelStation` (indexed lat/lon, unique per
  truckstop/address).
* `fuel/management/commands/import_fuel_prices.py` – CSV import: dedupe,
  min price, geocoding with city fallback, resumable.
* `fuel/services/optimizer.py` – the dynamic program (mention we'll come back
  to it).
* `fuel/services/routing.py` – the service layer shared by the API and the map
  (this is why the map never re-calls the external API).
* `fuel/services/ors.py` – ORS client with retries and caching.
* `Dockerfile`, `docker-compose.yml`, `entrypoint.sh` – deployment.

## 1:30 – 2:40 · API demo (full tank)

In Postman (or curl), send:

```
POST {{base_url}}/api/route/
Content-Type: application/json

{
  "start": {"address": "Los Angeles, CA"},
  "finish": {"address": "New York, NY"},
  "start_full_tank": true
}
```

Talking points while scrolling the JSON:

* `route` – GeoJSON LineString of the 2,790-mile route.
* `total_distance_miles`, `route_duration_hours`.
* `fuel_stops` – each stop shows the station, price, gallons purchased, cost,
  distance from the start and the detour distance. Note that a stop's
  `gallons_purchased` includes the fuel burned on the detour.
* `total_fuel_cost` – the money spent on fuel during the trip (the initial
  full tank is not charged).
* `map_url` – ready-made link for the map page.

Mention the caching: run the same request again and point out it returns
instantly (route cached for 1 hour, geocoding for 24 hours).

Optional curl equivalent:

```bash
curl -s -X POST http://localhost:8000/api/route/ \
  -H 'Content-Type: application/json' \
  -d '{"start": {"address": "Los Angeles, CA"}, "finish": {"address": "New York, NY"}}' | head -c 600
```

## 2:40 – 3:40 · Map endpoint

Switch to the browser tab with
`/map/?start=Los+Angeles,+CA&finish=New+York,+NY`.

* The blue line is the route; the orange numbered markers are the optimal fuel
  stops (in order), green/red are start/finish.
* Click a stop in the sidebar or on the map to show the popup with price,
  gallons and cost.
* Point out the panel header: distance, total fuel cost, gallons purchased.
* Explain: the map view calls the same service function as the API, so it
  reused the cached route instead of calling ORS again.

## 3:40 – 4:20 · Empty-tank start

Send a second request with `"start_full_tank": false` (Postman request
"Route – empty tank (coordinates)") or open:

```
/map/?start=Los+Angeles,+CA&finish=New+York,+NY&start_full_tank=false
```

* The first stop now appears at mile ~0 and is flagged `is_start_fuel: true`:
  the vehicle must buy fuel at a station near the start.
* Compare `total_fuel_cost` with the full-tank run – starting empty costs more
  because all 50 gallons of the initial tank are now paid for.
* If the start has no station within the 10-mile radius, the API returns a
  clear `422` error – that is the documented behaviour.

## 4:20 – 4:50 · How the algorithm works

* Candidate filtering: stations in the route's bounding box, projected onto
  the polyline; detour = 2 × distance to the route; only detours ≤ 10 miles
  survive.
* Dynamic programming over 0.5-gallon fuel levels:
  `dp[i][f]` = cheapest cost to be at station `i` with `f` gallons.
  Transitions optimise the purchase in closed form, so it stays fast even with
  hundreds of candidate stations.
* Detour handling: buying at a station also pays for the fuel burned on the
  detour (`detour_miles / 10 × price`), which is why a slightly cheaper
  station far off the route can lose to a closer one.
* If the finish cannot be reached, the API returns
  `422 no_feasible_fuel_plan` instead of a wrong answer.

## 4:50 – 5:00 · Conclusion

* Run it yourself: `docker compose up --build`, `python manage.py import_fuel_prices`,
  then hit `/api/route/` or `/map/`.
* Tests: `python manage.py test` (46 tests, including a brute-force
  cross-check of the optimiser).
* Deployment: the Dockerfile is ready for Dokploy – set the environment
  variables (`DATABASE_URL`, `SECRET_KEY`, `OPENROUTESERVICE_API_KEY`,
  `ALLOWED_HOSTS`) and expose port 8000.
