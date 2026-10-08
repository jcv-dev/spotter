"""Tests for the routing/geocoding provider dispatcher."""

from unittest import mock

from django.core.cache import cache
from django.test import TestCase, override_settings

from fuel.services import maps
from fuel.services.errors import (
    ProviderConfigurationError,
    ProviderQuotaError,
    ProviderRequestError,
)
from fuel.services.ors import DirectionsResult, GeocodeResult


@override_settings(ROUTING_PROVIDER="ors", GEOCODING_PROVIDER="ors")
class DispatcherTests(TestCase):
    def setUp(self):
        cache.clear()

    # ------------------------------------------------------------ geocoding ---
    @override_settings(GEOCODING_PROVIDER="auto")
    def test_geocode_auto_falls_back_to_nominatim_on_quota(self):
        fallback = GeocodeResult(1.0, 2.0, label="fallback")
        with mock.patch(
            "fuel.services.ors.geocode", side_effect=ProviderQuotaError("quota exceeded")
        ) as ors_mock, mock.patch(
            "fuel.services.nominatim.geocode", return_value=fallback
        ) as nominatim_mock:
            result = maps.geocode("Somewhere")
        self.assertEqual(result, fallback)
        ors_mock.assert_called_once()
        nominatim_mock.assert_called_once()

    @override_settings(GEOCODING_PROVIDER="auto")
    def test_geocode_auto_uses_ors_when_available(self):
        primary = GeocodeResult(3.0, 4.0, label="ors")
        with mock.patch("fuel.services.ors.geocode", return_value=primary) as ors_mock, mock.patch(
            "fuel.services.nominatim.geocode"
        ) as nominatim_mock:
            result = maps.geocode("Somewhere")
        self.assertEqual(result, primary)
        ors_mock.assert_called_once()
        nominatim_mock.assert_not_called()

    @override_settings(GEOCODING_PROVIDER="ors")
    def test_geocode_explicit_ors_does_not_fall_back(self):
        with mock.patch(
            "fuel.services.ors.geocode", side_effect=ProviderQuotaError("quota exceeded")
        ), mock.patch("fuel.services.nominatim.geocode") as nominatim_mock:
            with self.assertRaises(ProviderQuotaError):
                maps.geocode("Somewhere")
        nominatim_mock.assert_not_called()

    @override_settings(GEOCODING_PROVIDER="nominatim")
    def test_geocode_explicit_nominatim_skips_ors(self):
        fallback = GeocodeResult(5.0, 6.0, label="nominatim")
        with mock.patch("fuel.services.ors.geocode") as ors_mock, mock.patch(
            "fuel.services.nominatim.geocode", return_value=fallback
        ) as nominatim_mock:
            result = maps.geocode("Somewhere")
        self.assertEqual(result, fallback)
        ors_mock.assert_not_called()
        nominatim_mock.assert_called_once()

    # ------------------------------------------------------------- routing ---
    @override_settings(ROUTING_PROVIDER="auto")
    def test_directions_auto_falls_back_to_osrm(self):
        fallback = DirectionsResult(coordinates=[(1.0, 2.0), (3.0, 4.0)], distance_miles=5.0, duration_seconds=1.0)
        with mock.patch(
            "fuel.services.ors.get_directions",
            side_effect=ProviderConfigurationError("no api key"),
        ) as ors_mock, mock.patch(
            "fuel.services.osrm.get_directions", return_value=fallback
        ) as osrm_mock:
            result = maps.get_directions((1.0, 2.0), (3.0, 4.0))
        self.assertEqual(result, fallback)
        ors_mock.assert_called_once()
        osrm_mock.assert_called_once()

    @override_settings(ROUTING_PROVIDER="ors")
    def test_directions_explicit_ors_propagates_errors(self):
        with mock.patch(
            "fuel.services.ors.get_directions", side_effect=ProviderQuotaError("quota")
        ), mock.patch("fuel.services.osrm.get_directions") as osrm_mock:
            with self.assertRaises(ProviderQuotaError):
                maps.get_directions((1.0, 2.0), (3.0, 4.0))
        osrm_mock.assert_not_called()

    @override_settings(GEOCODING_PROVIDER="auto")
    def test_geocode_auto_fallback_is_sticky(self):
        fallback = GeocodeResult(1.0, 2.0, label="fallback")
        with mock.patch(
            "fuel.services.ors.geocode", side_effect=ProviderQuotaError("quota exceeded")
        ) as ors_mock, mock.patch(
            "fuel.services.nominatim.geocode", return_value=fallback
        ) as nominatim_mock:
            first = maps.geocode("Somewhere")
            second = maps.geocode("Somewhere")
            third = maps.geocode("Somewhere")
        self.assertEqual(first, fallback)
        self.assertEqual(second, fallback)
        self.assertEqual(third, fallback)
        # ORS is probed once; the rest of the cooldown goes straight to the
        # free provider instead of paying for a failing call on every request.
        ors_mock.assert_called_once()
        self.assertEqual(nominatim_mock.call_count, 3)

    @override_settings(GEOCODING_PROVIDER="auto")
    def test_geocode_auto_probes_ors_again_after_cooldown(self):
        fallback = GeocodeResult(1.0, 2.0, label="fallback")
        with mock.patch(
            "fuel.services.ors.geocode", side_effect=ProviderQuotaError("quota exceeded")
        ) as ors_mock, mock.patch(
            "fuel.services.nominatim.geocode", return_value=fallback
        ):
            maps.geocode("Somewhere")
            # Simulate the cooldown key expiring (or the quota resetting).
            cache.delete("maps:provider-down:ors:geocoding")
            maps.geocode("Somewhere")
        self.assertEqual(ors_mock.call_count, 2)

    @override_settings(GEOCODING_PROVIDER="auto")
    def test_geocode_auto_success_clears_sticky_fallback(self):
        fallback = GeocodeResult(1.0, 2.0, label="fallback")
        primary = GeocodeResult(3.0, 4.0, label="ors")
        with mock.patch(
            "fuel.services.nominatim.geocode", return_value=fallback
        ) as nominatim_mock, mock.patch("fuel.services.ors.geocode") as ors_mock:
            ors_mock.side_effect = ProviderQuotaError("quota exceeded")
            maps.geocode("Somewhere")
            # Quota reset: simulate cooldown expiry and a working ORS.
            cache.delete("maps:provider-down:ors:geocoding")
            ors_mock.side_effect = None
            ors_mock.return_value = primary
            self.assertEqual(maps.geocode("Somewhere"), primary)
            self.assertEqual(maps.geocode("Somewhere"), primary)
        # The successful probe clears the flag, so ORS keeps being used.
        ors_mock.assert_called()
        self.assertEqual(nominatim_mock.call_count, 1)

    def test_quota_failure_uses_full_cooldown_and_transient_a_short_one(self):
        with override_settings(MAPS_FALLBACK_COOLDOWN_SECONDS=3600):
            with mock.patch("fuel.services.maps.cache.set") as set_mock:
                maps._mark_down("ors", "geocoding", ProviderQuotaError("quota"))
                maps._mark_down("ors", "routing", ProviderRequestError("boom"))
        quota_ttl = set_mock.call_args_list[0].args[2]
        transient_ttl = set_mock.call_args_list[1].args[2]
        self.assertEqual(quota_ttl, 3600)
        self.assertLessEqual(transient_ttl, 60)

    @override_settings(ROUTING_PROVIDER="auto")
    def test_directions_auto_fallback_is_sticky(self):
        fallback = DirectionsResult(
            coordinates=[(1.0, 2.0), (3.0, 4.0)], distance_miles=5.0, duration_seconds=1.0
        )
        with mock.patch(
            "fuel.services.ors.get_directions", side_effect=ProviderQuotaError("quota")
        ) as ors_mock, mock.patch(
            "fuel.services.osrm.get_directions", return_value=fallback
        ) as osrm_mock:
            maps.get_directions((1.0, 2.0), (3.0, 4.0))
            maps.get_directions((1.0, 2.0), (3.0, 4.0))
        ors_mock.assert_called_once()
        self.assertEqual(osrm_mock.call_count, 2)

    @override_settings(ROUTING_PROVIDER="osrm")
    def test_directions_explicit_osrm_skips_ors(self):
        fallback = DirectionsResult(coordinates=[(1.0, 2.0), (3.0, 4.0)], distance_miles=5.0, duration_seconds=1.0)
        with mock.patch("fuel.services.ors.get_directions") as ors_mock, mock.patch(
            "fuel.services.osrm.get_directions", return_value=fallback
        ):
            result = maps.get_directions((1.0, 2.0), (3.0, 4.0))
        self.assertEqual(result, fallback)
        ors_mock.assert_not_called()

    # ------------------------------------------------------------- config ---
    @override_settings(GEOCODING_PROVIDER="bogus")
    def test_unknown_geocoding_provider_raises(self):
        with self.assertRaises(ProviderConfigurationError):
            maps.geocode("Somewhere")

    @override_settings(ROUTING_PROVIDER="bogus")
    def test_unknown_routing_provider_raises(self):
        with self.assertRaises(ProviderConfigurationError):
            maps.get_directions((1.0, 2.0), (3.0, 4.0))
