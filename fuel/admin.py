from django.contrib import admin

from .models import FuelStation


@admin.register(FuelStation)
class FuelStationAdmin(admin.ModelAdmin):
    list_display = ("name", "city", "state", "price", "latitude", "longitude", "is_approximate")
    list_filter = ("state", "is_approximate")
    search_fields = ("name", "address", "city")
    raw_id_fields = ()
