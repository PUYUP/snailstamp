import re

with open("src/snailstamp/apps/ledger/services.py", "r") as f:
    code = f.read()

code = code.replace(
    'def mint_batch(entry_id, issuer_id, issuer_member_id, batch=10_000):',
    'def mint_batch(entry_id, issuer_id, issuer_member_id, batch=10_000, prefix=""): '
)
code = code.replace(
    'SELECT ledger_mint_batch(%s, %s, %s, %s)", [entry_id, issuer_id, issuer_member_id, batch]',
    'SELECT ledger_mint_batch(%s, %s, %s, %s, %s)", [entry_id, issuer_id, issuer_member_id, batch, prefix]'
)

code = code.replace(
    'def mint_all(entry_id, issuer_id, issuer_member_id, batch=10_000):',
    'def mint_all(entry_id, issuer_id, issuer_member_id, batch=10_000, prefix=""): '
)
code = code.replace(
    'mint_batch(entry_id, issuer_id, issuer_member_id, batch)',
    'mint_batch(entry_id, issuer_id, issuer_member_id, batch, prefix)'
)

code = code.replace(
    'def create_item(owner_id, owner_member_id, reason, quantity, kind_code, metadata=None):',
    'def create_item(owner_id, owner_member_id, reason, quantity, kind_code, metadata=None, prefix=""): '
)
code = code.replace(
    'mint_all(entry_id, owner_id, owner_member_id)',
    'mint_all(entry_id, owner_id, owner_member_id, batch=10_000, prefix=prefix)'
)

with open("src/snailstamp/apps/ledger/services.py", "w") as f:
    f.write(code)
print("Done patching services for serial_no!")
