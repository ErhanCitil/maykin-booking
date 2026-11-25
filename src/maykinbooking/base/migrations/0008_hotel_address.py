from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("base", "0007_room_upload"),
    ]

    operations = [
        migrations.AddField(
            model_name="hotel",
            name="address",
            field=models.CharField(blank=True, default="", max_length=200),
        ),
    ]
