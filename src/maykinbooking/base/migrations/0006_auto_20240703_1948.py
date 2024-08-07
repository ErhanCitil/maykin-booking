# Generated by Django 3.2.21 on 2024-07-03 17:48

import django.core.files.storage
from django.db import migrations, models
import pathlib


class Migration(migrations.Migration):

    dependencies = [
        ('base', '0005_alter_hotel_image'),
    ]

    operations = [
        migrations.AlterField(
            model_name='hotel',
            name='image',
            field=models.ImageField(blank=True, null=True, storage=django.core.files.storage.FileSystemStorage(base_url='/static/', location=pathlib.PurePosixPath('/Users/erhancitil/Desktop/maykin-booking/static')), upload_to=''),
        ),
        migrations.AlterField(
            model_name='room',
            name='image',
            field=models.ImageField(storage=django.core.files.storage.FileSystemStorage(base_url='/static/', location=pathlib.PurePosixPath('/Users/erhancitil/Desktop/maykin-booking/static')), upload_to=''),
        ),
    ]
