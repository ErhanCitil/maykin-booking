# Generated by Django 4.1.5 on 2023-02-03 13:33

import ckeditor.fields
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('base', '0028_remove_hotel_terms_remove_order_created_at'),
    ]

    operations = [
        migrations.AddField(
            model_name='hotel',
            name='terms',
            field=ckeditor.fields.RichTextField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='order',
            name='created_at',
            field=models.DateTimeField(auto_now_add=True, null=True),
        ),
    ]