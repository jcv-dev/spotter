from rest_framework import serializers


class LocationSerializer(serializers.Serializer):
    """Either ``{"address": "..."}`` or ``{"lat": ..., "lon": ...}``."""

    address = serializers.CharField(required=False, allow_blank=False, trim_whitespace=True)
    lat = serializers.FloatField(required=False, min_value=-90.0, max_value=90.0)
    lon = serializers.FloatField(required=False, min_value=-180.0, max_value=180.0)

    def validate(self, attrs):
        has_address = bool(attrs.get("address"))
        has_lat = attrs.get("lat") is not None
        has_lon = attrs.get("lon") is not None
        if has_lat != has_lon:
            raise serializers.ValidationError("Provide both 'lat' and 'lon'.")
        if has_address and has_lat:
            raise serializers.ValidationError(
                "Provide either 'address' or 'lat'/'lon', not both."
            )
        if not has_address and not has_lat:
            raise serializers.ValidationError(
                "Provide either 'address' or both 'lat' and 'lon'."
            )
        return attrs


class RouteRequestSerializer(serializers.Serializer):
    start = LocationSerializer()
    finish = LocationSerializer()
    start_full_tank = serializers.BooleanField(default=True)
