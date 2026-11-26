"""
The admin of billing: invoices, payments and discount codes are read-only (they
are made by the services, and invoices are immutable); VAT rates are only for
the superuser. Price rules, seasons and discount codes are managed in the
dashboard.
"""
from django.contrib import admin
from django.urls import NoReverseMatch, reverse
from django.utils.html import format_html
from django.utils.translation import gettext_lazy as _

from .models import (
    DiscountCode,
    DiscountRedemption,
    Invoice,
    InvoiceLine,
    Payment,
    Refund,
    VatRate,
)


class ReadOnlyAdminMixin:
    """Nothing can be added, changed or deleted; viewing needs ``view_*``."""

    def get_readonly_fields(self, request, obj=None):
        return [field.name for field in self.model._meta.fields]

    def has_add_permission(self, request, obj=None):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


class InvoiceLineInline(ReadOnlyAdminMixin, admin.TabularInline):
    model = InvoiceLine
    extra = 0
    can_delete = False
    fields = (
        "position",
        "kind",
        "date",
        "description",
        "amount_excl",
        "amount_incl",
        "revenue_excl",
    )


@admin.register(Invoice)
class InvoiceAdmin(ReadOnlyAdminMixin, admin.ModelAdmin):
    list_display = (
        "number",
        "kind",
        "issue_date",
        "customer_name",
        "hotel_name",
        "total_incl",
        "is_test",
    )
    list_filter = ("kind", "is_test", "issue_date")
    search_fields = ("number", "customer_name", "customer_email")
    list_select_related = ("order",)
    inlines = [InvoiceLineInline]

    def get_readonly_fields(self, request, obj=None):
        # The PDF field itself is left out: its storage has no URL (the admin
        # would fail on it), the file is handed out by the dashboard.
        fields = [
            name
            for name in super().get_readonly_fields(request, obj)
            if name not in ("pdf", "pdf_sha256")
        ]
        return fields + ["pdf_file"]

    def get_fields(self, request, obj=None):
        return self.get_readonly_fields(request, obj)

    @admin.display(description=_("pdf"))
    def pdf_file(self, invoice):
        if not invoice.pdf:
            return _("(nog niet opgeslagen)")
        try:
            url = reverse("dashboard:invoice_pdf", args=[invoice.pk])
        except NoReverseMatch:
            return invoice.filename
        return format_html('<a href="{}">{}</a>', url, invoice.filename)

    def get_actions(self, request):
        # Not even the bulk delete action.
        return {}


class RefundInline(ReadOnlyAdminMixin, admin.TabularInline):
    model = Refund
    extra = 0
    can_delete = False
    fields = ("created_at", "amount", "status", "reason", "attempts", "error")


@admin.register(Payment)
class PaymentAdmin(ReadOnlyAdminMixin, admin.ModelAdmin):
    list_display = (
        "order",
        "provider",
        "provider_id",
        "status",
        "amount",
        "created_at",
    )
    list_filter = ("provider", "status")
    search_fields = ("provider_id", "reference", "order__id")
    inlines = [RefundInline]


class DiscountRedemptionInline(ReadOnlyAdminMixin, admin.TabularInline):
    model = DiscountRedemption
    extra = 0
    can_delete = False
    fields = ("order", "amount", "created_at", "released_at")


@admin.register(DiscountCode)
class DiscountCodeAdmin(ReadOnlyAdminMixin, admin.ModelAdmin):
    list_display = (
        "code",
        "kind",
        "value",
        "valid_from",
        "valid_until",
        "times_used",
        "max_uses",
        "is_active",
    )
    list_filter = ("kind", "is_active")
    search_fields = ("code",)
    inlines = [DiscountRedemptionInline]


@admin.register(VatRate)
class VatRateAdmin(admin.ModelAdmin):
    """Only the superuser manages VAT rates."""

    list_display = ("country", "rate", "valid_from", "note")
    list_filter = ("country",)

    def has_module_permission(self, request):
        return request.user.is_active and request.user.is_superuser

    def has_view_permission(self, request, obj=None):
        return request.user.is_active and request.user.is_superuser

    def has_add_permission(self, request):
        return request.user.is_active and request.user.is_superuser

    def has_change_permission(self, request, obj=None):
        return request.user.is_active and request.user.is_superuser

    def has_delete_permission(self, request, obj=None):
        return request.user.is_active and request.user.is_superuser
