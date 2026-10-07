from django.db import models


class FuelStation(models.Model):
    """A truck stop selling diesel/petrol, located via its address."""

    name = models.CharField(max_length=255)
    address = models.CharField(max_length=255)
    city = models.CharField(max_length=128)
    state = models.CharField(max_length=2)
    latitude = models.FloatField(db_index=True)
    longitude = models.FloatField(db_index=True)
    price = models.DecimalField(max_digits=6, decimal_places=3, help_text="Retail price (USD/gallon)")
    truckstop_id = models.IntegerField(null=True, blank=True)
    rack_id = models.IntegerField(null=True, blank=True)
    # True when only the city/state could be geocoded (highway-exit addresses
    # are often not resolvable to a street address).
    is_approximate = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["state", "city", "name"]
        constraints = [
            models.UniqueConstraint(
                fields=["truckstop_id", "address", "city", "state"],
                name="unique_station_per_truckstop_address",
            )
        ]

    def __str__(self) -> str:
        return f"{self.name} ({self.city}, {self.state}) - ${self.price}"
