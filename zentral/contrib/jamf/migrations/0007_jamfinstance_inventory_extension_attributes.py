# Generated by Django 2.2.27 on 2022-03-17 12:09

import django.contrib.postgres.fields
import django.core.validators
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('jamf', '0006_jamfinstance_bearer_token_authentication'),
    ]

    operations = [
        migrations.AddField(
            model_name='jamfinstance',
            name='inventory_extension_attributes',
            field=django.contrib.postgres.fields.ArrayField(
                base_field=models.CharField(max_length=256, validators=[django.core.validators.MinLengthValidator(1)]),
                blank=True, default=list,
                help_text='Comma separated list of the extension attributes to collect as inventory extra facts',
                size=None
            ),
        ),
    ]