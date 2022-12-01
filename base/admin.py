from django.contrib import admin
from .models import Data
from .models import ContactForm
# Register your models here.
# Hier mee registreer ik de Database model in de admin pagina.
admin.site.register(Data)
admin.site.register(ContactForm)