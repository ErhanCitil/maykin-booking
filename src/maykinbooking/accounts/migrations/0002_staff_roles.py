"""
The staff roles "Receptie" and "Beheer" as groups with their permissions.

Existing staff members who are not superusers get "Beheer", so nobody loses
access to the dashboard. The permissions are a frozen copy of
``accounts/roles.ROLE_PERMISSIONS`` at the time of this migration.
"""
from django.contrib.auth.management import create_permissions
from django.contrib.contenttypes.management import create_contenttypes
from django.db import migrations

RECEPTION = (
    "base.view_order",
    "base.cancel_order",
    "base.resend_order_email",
)
ROLE_PERMISSIONS = {
    "Receptie": RECEPTION,
    "Beheer": RECEPTION
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
EXISTING_STAFF_ROLE = "Beheer"


def create_roles(apps, schema_editor):
    using = schema_editor.connection.alias
    # The permissions are normally created after all migrations (post_migrate);
    # this migration needs them now.
    for app_config in apps.get_app_configs():
        app_config.models_module = True
        create_contenttypes(app_config, verbosity=0, using=using, apps=apps)
        create_permissions(app_config, verbosity=0, using=using, apps=apps)
        app_config.models_module = None

    Group = apps.get_model("auth", "Group")
    Permission = apps.get_model("auth", "Permission")
    User = apps.get_model("accounts", "User")
    for name, labels in ROLE_PERMISSIONS.items():
        group, _created = Group.objects.using(using).get_or_create(name=name)
        permissions = []
        for label in labels:
            app_label, codename = label.split(".")
            permissions.append(
                Permission.objects.using(using).get(
                    content_type__app_label=app_label, codename=codename
                )
            )
        group.permissions.set(permissions)

    management = Group.objects.using(using).get(name=EXISTING_STAFF_ROLE)
    for user in User.objects.using(using).filter(is_staff=True, is_superuser=False):
        user.groups.add(management)


def remove_roles(apps, schema_editor):
    Group = apps.get_model("auth", "Group")
    Group.objects.using(schema_editor.connection.alias).filter(
        name__in=ROLE_PERMISSIONS
    ).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("accounts", "0001_initial"),
        ("auth", "0012_alter_user_first_name_max_length"),
        ("contenttypes", "0002_remove_content_type_name"),
        ("base", "0016_phase2_constraints"),
        ("billing", "0002_seed"),
    ]

    operations = [
        migrations.RunPython(create_roles, remove_roles),
    ]
