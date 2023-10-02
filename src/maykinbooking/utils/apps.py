from django.apps import AppConfig


class UtilsConfig(AppConfig):
    name = "maykinbooking.utils"

    def ready(self):
        from . import checks  # noqa
