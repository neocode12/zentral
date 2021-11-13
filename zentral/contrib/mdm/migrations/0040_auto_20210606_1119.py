# Generated by Django 2.2.24 on 2021-06-06 11:19

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('mdm', '0039_auto_20210603_1620'),
    ]

    operations = [
        migrations.RemoveIndex(
            model_name='enterpriseapp',
            name='mdm_enterpr_bundle__777841_idx',
        ),
        migrations.RenameField(
            model_name='enterpriseapp',
            old_name='bundle_identifier',
            new_name='product_id',
        ),
        migrations.RenameField(
            model_name='enterpriseapp',
            old_name='bundle_version',
            new_name='product_version',
        ),
        migrations.AddIndex(
            model_name='enterpriseapp',
            index=models.Index(fields=['product_id', 'product_version'], name='mdm_enterpr_product_a9f446_idx'),
        ),
    ]