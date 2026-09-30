from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('paypal', '0004_increase_max_char_length_status'),
    ]

    operations = [
        migrations.AddField(
            model_name='expresscheckouttransaction',
            name='order_number',
            field=models.CharField(blank=True, db_index=True, max_length=128),
        ),
        migrations.AddField(
            model_name='expresscheckouttransaction',
            name='basket_id',
            field=models.PositiveIntegerField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='expresscheckouttransaction',
            name='capture_status',
            field=models.CharField(blank=True, max_length=32),
        ),
        migrations.AddField(
            model_name='expresscheckouttransaction',
            name='tracking_number',
            field=models.CharField(blank=True, max_length=128),
        ),
        migrations.AddField(
            model_name='expresscheckouttransaction',
            name='carrier',
            field=models.CharField(blank=True, max_length=64),
        ),
        migrations.AlterField(
            model_name='expresscheckouttransaction',
            name='address_full_name',
            field=models.CharField(blank=True, max_length=255),
        ),
        migrations.AlterField(
            model_name='expresscheckouttransaction',
            name='address',
            field=models.TextField(blank=True),
        ),
    ]
