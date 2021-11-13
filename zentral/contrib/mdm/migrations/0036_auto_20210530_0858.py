# Generated by Django 2.2.18 on 2021-05-30 08:58

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('mdm', '0035_auto_20210530_0717'),
    ]

    operations = [
        migrations.AlterField(
            model_name='enterpriseapp',
            name='bundle_identifier',
            field=models.TextField(),
        ),
        migrations.AlterField(
            model_name='profile',
            name='payload_description',
            field=models.TextField(default=''),
            preserve_default=False,
        ),
        migrations.AlterField(
            model_name='profile',
            name='payload_identifier',
            field=models.TextField(db_index=True),
        ),
        migrations.AlterUniqueTogether(
            name='artifactversion',
            unique_together={('artifact', 'version')},
        ),
        migrations.AlterUniqueTogether(
            name='enterpriseapp',
            unique_together=set(),
        ),
        migrations.AddIndex(
            model_name='enterpriseapp',
            index=models.Index(fields=['bundle_identifier', 'bundle_version'], name='mdm_enterpr_bundle__777841_idx'),
        ),
        migrations.RemoveField(
            model_name='enterpriseapp',
            name='created_at',
        ),
        migrations.RemoveField(
            model_name='enterpriseapp',
            name='updated_at',
        ),
    ]