from django.contrib import admin

from import_export.admin import ImportExportModelAdmin

from .models import (
    City,
    EmailLog,
    Highlight,
    Hotel,
    Order,
    OrderNight,
    OrderStatusChange,
    Room,
)


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


class ReadOnlyInline(admin.TabularInline):
    """History of an order: only to look at, it is written by the booking service."""

    extra = 0
    can_delete = False

    def get_readonly_fields(self, request, obj=None):
        return self.fields

    def has_add_permission(self, request, obj=None):
        return False

    def has_change_permission(self, request, obj=None):
        return False


class OrderNightInline(ReadOnlyInline):
    model = OrderNight
    fields = ("date", "price")


class OrderStatusChangeInline(ReadOnlyInline):
    model = OrderStatusChange
    fields = (
        "created_at",
        "field",
        "old_value",
        "new_value",
        "actor_type",
        "actor_label",
        "reason",
    )


class EmailLogInline(ReadOnlyInline):
    model = EmailLog
    fields = (
        "created_at",
        "kind",
        "recipient",
        "subject",
        "status",
        "sent_at",
        "triggered_by",
        "error",
    )


class OrderAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "hotel",
        "room",
        "start_date",
        "end_date",
        "status",
        "payment_status",
        "total_price",
    )
    list_filter = ("status", "payment_status")
    list_select_related = ("hotel", "room")
    # Bookings and status changes go through the booking service (availability
    # check, price snapshot, log), not the admin. Guest details can be corrected.
    readonly_fields = (
        "token",
        "created_at",
        "hotel",
        "room",
        "start_date",
        "end_date",
        "status",
        "payment_status",
        "hold_expires_at",
        "total_price",
    )
    inlines = [OrderNightInline, OrderStatusChangeInline, EmailLogInline]

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        # Bookings are cancelled (with a log and a mail), never deleted.
        return False


admin.site.register(Hotel, HotelAdmin)
admin.site.register(City, CityAdmin)
admin.site.register(Order, OrderAdmin)
admin.site.register(Highlight)
