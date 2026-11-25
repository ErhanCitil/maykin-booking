from django.contrib import admin

from import_export.admin import ImportExportModelAdmin

from .models import *


# Register your models here.
# Hier mee registreer ik de Database model in de admin pagina.
class CityAdmin(ImportExportModelAdmin, admin.ModelAdmin):
    pass


class RoomAdmin(admin.TabularInline):
    model = Room


class HotelAdmin(ImportExportModelAdmin, admin.ModelAdmin):
    inlines = [
        RoomAdmin,
    ]


admin.site.register(Hotel, HotelAdmin)
admin.site.register(City, CityAdmin)
admin.site.register(Order)
admin.site.register(Highlight)
