# Generated by Django 3.1.6 on 2021-02-12 21:14

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('merch', '0001_initial'),
    ]

    operations = [
        migrations.AlterUniqueTogether(
            name='merchorderitem',
            unique_together={('item', 'order')},
        ),
    ]