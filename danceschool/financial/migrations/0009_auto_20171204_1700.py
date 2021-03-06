# -*- coding: utf-8 -*-
# Generated by Django 1.10.7 on 2017-12-04 22:00
from __future__ import unicode_literals

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('financial', '0008_revenueitem_buyerpayssalestax'),
    ]

    operations = [
        migrations.AddField(
            model_name='repeatedexpenserule',
            name='advanceDaysReference',
            field=models.CharField(choices=[('S', 'First occurrence starts'), ('E', 'Last occurrence ends')], default='S', max_length=1, verbose_name='in advance of'),
        ),
        migrations.AddField(
            model_name='repeatedexpenserule',
            name='priorDaysReference',
            field=models.CharField(choices=[('S', 'First occurrence starts'), ('E', 'Last occurrence ends')], default='E', max_length=1, verbose_name='prior to'),
        ),
    ]
