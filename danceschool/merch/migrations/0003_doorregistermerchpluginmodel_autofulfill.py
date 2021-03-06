# Generated by Django 3.1.6 on 2021-02-19 16:51

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('merch', '0002_auto_20210212_1614'),
    ]

    operations = [
        migrations.AddField(
            model_name='doorregistermerchpluginmodel',
            name='autoFulfill',
            field=models.BooleanField(default=False, help_text='If checked, and if all items added to this merch order also this option check, then the order will automatically be marked as fulfilled when the invoice is finalized. Useful for merchandise that is sold immediately at the point-of-sale.', verbose_name='Automatically mark order as fulfilled upon payment.'),
        ),
    ]
