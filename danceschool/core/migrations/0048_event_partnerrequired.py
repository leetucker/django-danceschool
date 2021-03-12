# Generated by Django 3.1.6 on 2021-03-08 18:11

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0047_auto_20210226_1705'),
    ]

    operations = [
        migrations.AddField(
            model_name='event',
            name='partnerRequired',
            field=models.BooleanField(default=False, help_text='If checked, then customers will be prompted to enter the name of their partner when registering.', verbose_name='Partner required'),
        ),
    ]