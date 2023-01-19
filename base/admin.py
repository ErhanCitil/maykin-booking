from django.contrib import admin
from .models import *
from import_export.admin import ImportExportModelAdmin
# Register your models here.
# Hier mee registreer ik de Database model in de admin pagina.
class CityAdmin(ImportExportModelAdmin, admin.ModelAdmin):
    pass    

class RoomAdmin(admin.TabularInline):
    model = Room

class HotelAdmin(ImportExportModelAdmin, admin.ModelAdmin):
    inlines = [RoomAdmin,]

admin.site.register(Hotel, HotelAdmin)
admin.site.register(City, CityAdmin)