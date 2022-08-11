# Generated by Django 3.2.14 on 2022-08-04 06:04

import accounts.models
from django.conf import settings
from django.db import connection, migrations, models, transaction
import django.db.models.deletion


def migrate_api_tokens(apps, schema_editor):
    APIToken = apps.get_model("accounts", "APIToken")
    User = apps.get_model("accounts", "User")
    cursor = connection.cursor()
    sid = transaction.savepoint()
    try:
        cursor.execute("SELECT user_id, key FROM authtoken_token")
    except Exception:
        transaction.savepoint_rollback(sid)
        return
    else:
        transaction.savepoint_commit(sid)
    for user_id, key in cursor.fetchall():
        APIToken.objects.get_or_create(
            user=User.objects.get(pk=user_id),
            defaults={"hashed_key": APIToken.objects._hash_key(key)},
        )
    cursor.execute("DROP TABLE IF EXISTS authtoken_token")


class Migration(migrations.Migration):

    dependencies = [
        ('accounts', '0015_alter_user_first_name'),
    ]

    operations = [
        migrations.CreateModel(
            name='APIToken',
            fields=[
                ('hashed_key', models.CharField(max_length=64, primary_key=True, serialize=False)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('user', models.OneToOneField(on_delete=django.db.models.deletion.CASCADE,
                                              related_name='api_token',
                                              to=settings.AUTH_USER_MODEL)),
            ],
            managers=[
                ('objects', accounts.models.APITokenManager()),
            ],
        ),
        migrations.RunPython(migrate_api_tokens),
    ]