"""ledger_block_anchors: indeks checkpoint blok yang diterbitkan ke backend anchor eksternal."""
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ('ledger', '0009_multi_region_sealer_and_shamir'),
    ]

    operations = [
        migrations.CreateModel(
            name='BlockAnchor',
            fields=[
                ('id', models.BigAutoField(primary_key=True, serialize=False)),
                ('block_no', models.BigIntegerField()),
                ('block_hash', models.CharField(max_length=64)),
                ('backend', models.CharField(max_length=64)),
                ('receipt', models.TextField(blank=True, default='')),
                ('anchored_at', models.DateTimeField(auto_now_add=True)),
            ],
            options={
                'db_table': 'ledger_block_anchors',
                'ordering': ['-block_no'],
                'constraints': [
                    models.UniqueConstraint(fields=('block_no', 'backend'), name='ledger_anchor_unique'),
                ],
            },
        ),
    ]
