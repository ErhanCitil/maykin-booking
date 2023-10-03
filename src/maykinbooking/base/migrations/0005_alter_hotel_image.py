# Generated by Django 3.2.21 on 2023-10-03 08:30

import django.core.files.storage
from django.db import migrations, models
import pathlib


class Migration(migrations.Migration):

    dependencies = [
        ('base', '0004_alter_room_image'),
    ]

    operations = [
        migrations.AlterField(
            model_name='hotel',
            name='image',
            field=models.ImageField(blank=True, null=True, storage=django.core.files.storage.FileSystemStorage(base_url='/static/', location=pathlib.PurePosixPath('/home/erhan/Bureaublad/maykin-booking/static')), upload_to=''),
        ),
    ]
