from django.contrib import admin
from .models import City, Hotel
# Register your models here.
# Hier mee registreer ik de Database model in de admin pagina.
admin.site.register(City)
admin.site.register(Hotel)