# Generated by Django 4.1.5 on 2023-01-25 13:40

from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('base', '0020_alter_order_end_date_alter_order_start_date'),
    ]

    operations = [
        migrations.AlterField(
            model_name='order',
            name='hotel',
            field=models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='order', to='base.hotel'),
        ),
        migrations.AlterField(
            model_name='order',
            name='room',
            field=models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='room', to='base.room'),
        ),
    ]