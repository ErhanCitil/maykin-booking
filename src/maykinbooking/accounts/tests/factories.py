from django.contrib.auth import get_user_model

import factory
from factory.django import DjangoModelFactory

from maykinbooking.accounts.roles import MANAGEMENT, RECEPTION, role_group

User = get_user_model()


class UserFactory(DjangoModelFactory):
    username = factory.Sequence(lambda n: f"user-{n}")
    first_name = factory.Faker("first_name")
    last_name = factory.Faker("last_name")
    password = factory.PostGenerationMethodCall("set_password", "password")

    class Meta:
        model = User


class StaffUserFactory(UserFactory):
    is_staff = True


class SuperUserFactory(StaffUserFactory):
    is_superuser = True


class RolelessStaffUserFactory(StaffUserFactory):
    """Staff without a role: can only open the dashboard home page."""


class ReceptionUserFactory(StaffUserFactory):
    """Staff with the role "Receptie": bookings only."""

    @factory.post_generation
    def role(self, create, extracted, **kwargs):
        if create:
            self.groups.add(role_group(RECEPTION))


class ManagerUserFactory(StaffUserFactory):
    """Staff with the role "Beheer": the whole dashboard."""

    @factory.post_generation
    def role(self, create, extracted, **kwargs):
        if create:
            self.groups.add(role_group(MANAGEMENT))
