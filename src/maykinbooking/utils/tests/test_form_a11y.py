"""
Invalid form fields are announced by screen readers: the widget gets
aria-invalid and aria-describedby, pointing at the error message.
"""
from datetime import timedelta

from django import forms
from django.template import Context, Template
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from maykinbooking.accounts.tests.factories import StaffUserFactory
from maykinbooking.base.tests.factories import CityFactory, OrderFactory


class NameForm(forms.Form):
    name = forms.CharField()


def render(template, **context):
    return Template("{% load form_a11y %}" + template).render(Context(context))


class AccessibleWidgetTagTests(TestCase):
    def test_valid_field_is_rendered_as_usual(self):
        form = NameForm({"name": "Jan"})
        form.is_valid()
        html = render("{% accessible_widget form.name %}", form=form)
        self.assertEqual(html, str(form["name"]))
        self.assertNotIn("aria-", html)

    def test_invalid_field_points_at_its_errors(self):
        form = NameForm({"name": ""})
        form.is_valid()
        html = render("{% accessible_widget form.name %}", form=form)
        self.assertIn('aria-invalid="true"', html)
        self.assertIn('aria-describedby="id_name_errors"', html)
        self.assertIn('name="name"', html)

    def test_shared_error_message(self):
        form = NameForm({"name": "Jan"})
        form.is_valid()
        html = render(
            '{% accessible_widget form.name has_errors=True errors_id="all" %}',
            form=form,
        )
        self.assertIn('aria-invalid="true"', html)
        self.assertIn('aria-describedby="all"', html)


class DashboardFormTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.staff = StaffUserFactory()

    def setUp(self):
        self.client.force_login(self.staff)

    def test_fields_template_links_the_error_to_the_field(self):
        start = timezone.localdate() + timedelta(days=3)
        order = OrderFactory(start_date=start, end_date=start + timedelta(days=1))

        response = self.client.post(
            reverse("dashboard:booking_cancel", args=[order.pk]),
            {"reason": "", "confirm": "on"},
        )

        self.assertContains(
            response, 'aria-invalid="true" aria-describedby="id_reason_errors"'
        )
        self.assertContains(response, 'id="id_reason_errors"')
        # The confirmation is valid: no aria-invalid there.
        self.assertContains(response, 'aria-invalid="true"', count=1)

    def test_valid_form_has_no_aria_invalid(self):
        response = self.client.get(reverse("dashboard:booking_list"))
        self.assertNotContains(response, "aria-invalid")

    def test_booking_list_filter_links_the_error_to_the_field(self):
        response = self.client.get(
            reverse("dashboard:booking_list"), {"aankomst_van": "morgen"}
        )

        self.assertContains(
            response, 'aria-invalid="true" aria-describedby="id_aankomst_van_errors"'
        )
        self.assertContains(response, 'id="id_aankomst_van_errors"')


class CityPagePeriodTests(TestCase):
    def test_period_fields_point_at_the_shared_error_message(self):
        city = CityFactory(name="Amsterdam")
        today = timezone.localdate()

        response = self.client.get(
            reverse("stad", args=[city.name]),
            {
                "van": (today + timedelta(days=5)).isoformat(),
                "tot": (today + timedelta(days=2)).isoformat(),
            },
        )

        self.assertContains(response, 'id="period-errors"')
        self.assertContains(
            response, 'aria-invalid="true" aria-describedby="period-errors"', count=2
        )

    def test_valid_period_has_no_aria_invalid(self):
        city = CityFactory(name="Amsterdam")
        response = self.client.get(reverse("stad", args=[city.name]))
        self.assertNotContains(response, "aria-invalid")
