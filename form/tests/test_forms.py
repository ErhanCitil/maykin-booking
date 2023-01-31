from ..forms import FormContact, OrderForm1, OrderForm2
from django.test import TestCase

class OrderForm2Test(TestCase):
    def setUp(self):
        self.form = OrderForm2()

    def test_form_has_fields(self):
        expected = ['first_name', 'last_name', 'email', 'address', 'zipcode', 'country', 'terms']
        actual = list(self.form.fields)
        self.assertSequenceEqual(expected, actual)

    def test_terms_and_conditions(self):
        self.assertTrue(self.form.fields['terms'].required)