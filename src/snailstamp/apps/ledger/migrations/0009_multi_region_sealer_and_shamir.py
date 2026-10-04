"""
Multi-region sealer, audit log lengkap untuk key rotation, dan Shamir's Secret Sharing:

1. ledger_sealer_keys: kolom region, deactivated_at, previous_key (rantai rotasi)
2. ledger_sealer_audit_logs: kolom region + event key_split / key_recovered
3. ledger_block_signatures: co-signature blok dari sealer lain (ledger_blocks append-only)
"""
import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ('ledger', '0008_add_sealer_audit_log'),
    ]

    operations = [
        migrations.AddField(
            model_name='sealerkey',
            name='region',
            field=models.CharField(blank=True, db_index=True, default='', max_length=64),
        ),
        migrations.AddField(
            model_name='sealerkey',
            name='deactivated_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='sealerkey',
            name='previous_key',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT,
                                    related_name='successors', to='ledger.sealerkey'),
        ),
        migrations.AddField(
            model_name='sealerkeyauditlog',
            name='region',
            field=models.CharField(blank=True, default='', max_length=64),
        ),
        migrations.AlterField(
            model_name='sealerkeyauditlog',
            name='event_type',
            field=models.CharField(choices=[('key_registered', 'Key Registered'), ('key_rotated', 'Key Rotated'),
                                            ('key_expired', 'Key Expired'), ('key_revoked', 'Key Revoked'),
                                            ('key_used', 'Key Used'), ('key_split', 'Key Split (Shamir)'),
                                            ('key_recovered', 'Key Recovered (Shamir)')],
                                   max_length=50),
        ),
        migrations.CreateModel(
            name='BlockSignature',
            fields=[
                ('id', models.BigAutoField(primary_key=True, serialize=False)),
                ('region', models.CharField(blank=True, default='', max_length=64)),
                ('public_key', models.CharField(max_length=64)),
                ('signature', models.CharField(max_length=128)),
                ('signed_at', models.DateTimeField(auto_now_add=True)),
                ('block', models.ForeignKey(db_column='block_no', db_constraint=False,
                                            on_delete=django.db.models.deletion.DO_NOTHING,
                                            related_name='cosignatures', to='ledger.block')),
                ('sealer', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT,
                                             related_name='cosignatures', to='ledger.sealerkey')),
            ],
            options={
                'db_table': 'ledger_block_signatures',
                'ordering': ['block_id', 'signed_at'],
                'constraints': [models.UniqueConstraint(fields=('block', 'sealer'), name='ledger_blocksig_unique')],
            },
        ),
    ]
