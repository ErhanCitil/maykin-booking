"""
The roles of staff in the dashboard, as Django groups.

* "Receptie": see bookings, cancel them and send their mails again.
* "Beheer": everything of the reception, plus hotels and rooms, price rules,
  discount codes, invoices, customers and the reports.

The superuser can do everything. A staff member without a role can only see the
dashboard home page. Migration ``accounts/0002_staff_roles`` creates the groups
from a frozen copy of ``ROLE_PERMISSIONS``; a change here needs a new migration.
"""
from django.contrib.auth.models import Group, Permission

RECEPTION = "Receptie"
MANAGEMENT = "Beheer"

RECEPTION_PERMISSIONS = (
    "base.view_order",
    "base.cancel_order",
    "base.resend_order_email",
)

ROLE_PERMISSIONS = {
    RECEPTION: RECEPTION_PERMISSIONS,
    MANAGEMENT: RECEPTION_PERMISSIONS
    + (
        "base.view_hotel",
        "base.add_hotel",
        "base.change_hotel",
        "base.delete_hotel",
        "base.view_room",
        "base.add_room",
        "base.change_room",
        "base.delete_room",
        "base.view_city",
        "base.add_city",
        "base.view_roomblock",
        "base.add_roomblock",
        "base.delete_roomblock",
        "base.view_customers",
        "base.view_reports",
        "billing.view_pricingsettings",
        "billing.change_pricingsettings",
        "billing.view_seasonperiod",
        "billing.add_seasonperiod",
        "billing.change_seasonperiod",
        "billing.delete_seasonperiod",
        "billing.view_discountcode",
        "billing.add_discountcode",
        "billing.change_discountcode",
        "billing.view_invoice",
    ),
}


def role_permissions(name):
    """The ``Permission`` objects of the role ``name``."""
    permissions = []
    for label in ROLE_PERMISSIONS[name]:
        app_label, codename = label.split(".")
        permissions.append(
            Permission.objects.get(content_type__app_label=app_label, codename=codename)
        )
    return permissions


def role_group(name):
    """
    The group of the role ``name`` with exactly its permissions; created when
    it is missing (e.g. in tests after a database flush).
    """
    group, _created = Group.objects.get_or_create(name=name)
    group.permissions.set(role_permissions(name))
    return group
