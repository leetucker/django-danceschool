# Generated by Django 3.1.6 on 2021-02-20 05:56

from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('cms', '0022_auto_20180620_1551'),
        ('register', '0007_auto_20210217_1457'),
        ('merch', '0004_merchorder_data'),
    ]

    operations = [
        migrations.RenameModel(
            old_name='DoorRegisterMerchPluginModel',
            new_name='RegisterMerchPluginModel'
        ),
    ]
