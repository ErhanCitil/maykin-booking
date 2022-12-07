from django.shortcuts import render
from django.views import generic
from .forms import FormContact
from django.urls import reverse_lazy
# Create your views here.
class ContactSave(generic.FormView):
    template_name = 'contact.html'
    form_class = FormContact
    success_url = reverse_lazy('index')

    def form_valid(self, form):
        form.save()
        return super().form_valid(form)