from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("base", "0006_auto_20240703_1948"),
    ]

    operations = [
        migrations.AddField(
            model_name="room",
            name="upload",
            field=models.ImageField(blank=True, null=True, upload_to="room_img/"),
        ),
    ]
