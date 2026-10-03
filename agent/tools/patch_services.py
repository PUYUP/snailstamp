import re

with open("src/snailstamp/apps/ledger/services.py", "r") as f:
    code = f.read()

code = code.replace(
    'def create_entry(issuer_id, reason, supply, metadata=None, kind=0):',
    'def create_entry(issuer_id, issuer_member_id, reason, supply, metadata=None, kind=0):'
)
code = code.replace(
    'SELECT ledger_create_entry(%s, %s, %s, %s::jsonb, %s::smallint)",\n                 [issuer_id, reason, supply',
    'SELECT ledger_create_entry(%s, %s, %s, %s, %s::jsonb, %s::smallint)",\n                 [issuer_id, issuer_member_id, reason, supply'
)

code = code.replace(
    'def mint_batch(entry_id, issuer_id, batch=10_000):',
    'def mint_batch(entry_id, issuer_id, issuer_member_id, batch=10_000):'
)
code = code.replace(
    'SELECT ledger_mint_batch(%s, %s, %s)", [entry_id, issuer_id, batch]',
    'SELECT ledger_mint_batch(%s, %s, %s, %s)", [entry_id, issuer_id, issuer_member_id, batch]'
)

code = code.replace(
    'def mint_all(entry_id, issuer_id, batch=10_000):',
    'def mint_all(entry_id, issuer_id, issuer_member_id, batch=10_000):'
)
code = code.replace(
    'mint_batch(entry_id, issuer_id, batch)',
    'mint_batch(entry_id, issuer_id, issuer_member_id, batch)'
)

code = code.replace(
    'def create_item(owner_id, reason, quantity, kind_code, metadata=None):',
    'def create_item(owner_id, owner_member_id, reason, quantity, kind_code, metadata=None):'
)
code = code.replace(
    'create_entry(owner_id, reason, quantity, metadata, kind=kind_code)',
    'create_entry(owner_id, owner_member_id, reason, quantity, metadata, kind=kind_code)'
)
code = code.replace(
    'mint_all(entry_id, owner_id)',
    'mint_all(entry_id, owner_id, owner_member_id)'
)

code = code.replace(
    'def send(collection_id, actor_id, payload=None):',
    'def send(collection_id, actor_id, actor_member_id, payload=None):'
)
code = code.replace(
    'SELECT * FROM ledger_send(%s, %s, %s::jsonb)",\n                 [collection_id, actor_id, _json(payload)]',
    'SELECT * FROM ledger_send(%s, %s, %s, %s::jsonb)",\n                 [collection_id, actor_id, actor_member_id, _json(payload)]'
)

code = code.replace(
    'def claim_transfer(token, actor_id):',
    'def claim_transfer(token, actor_id, actor_member_id):'
)
code = code.replace(
    'SELECT ledger_claim_transfer(%s, %s)", [token, actor_id]',
    'SELECT ledger_claim_transfer(%s, %s, %s)", [token, actor_id, actor_member_id]'
)

code = code.replace(
    'def cancel_send(collection_id, actor_id):',
    'def cancel_send(collection_id, actor_id, actor_member_id):'
)
code = code.replace(
    'SELECT ledger_cancel_send(%s, %s)", [collection_id, actor_id]',
    'SELECT ledger_cancel_send(%s, %s, %s)", [collection_id, actor_id, actor_member_id]'
)

code = code.replace(
    'def use(collection_id, actor_id, action_code=None, payload=None):',
    'def use(collection_id, actor_id, actor_member_id, action_code=None, payload=None):'
)
code = code.replace(
    'SELECT ledger_use(%s, %s, %s::smallint, %s::jsonb)",\n                 [collection_id, actor_id, action_id, _json(payload)]',
    'SELECT ledger_use(%s, %s, %s, %s::smallint, %s::jsonb)",\n                 [collection_id, actor_id, actor_member_id, action_id, _json(payload)]'
)

code = code.replace(
    'def act(action_code, tool_id, target_id, actor_id, content=None, content_hash=None, payload=None):',
    'def act(action_code, tool_id, target_id, actor_id, actor_member_id, content=None, content_hash=None, payload=None):'
)
code = code.replace(
    'SELECT * FROM ledger_act(%s::smallint, %s, %s, %s, %s::bytea, %s::jsonb)",\n                       [ac.id, tool_id, target_id, actor_id, content_hash, _json(payload)]',
    'SELECT * FROM ledger_act(%s::smallint, %s, %s, %s, %s, %s::bytea, %s::jsonb)",\n                       [ac.id, tool_id, target_id, actor_id, actor_member_id, content_hash, _json(payload)]'
)

code = code.replace(
    '"event_type"]), str(f["actor_id"]),\n                     n(f["counterparty_id"])',
    '"event_type"]), str(f["actor_id"]), str(f["actor_member_id"]),\n                     n(f["counterparty_id"])'
)

code = code.replace(
    'SELECT l.collection_id, l.seq, l.event_type, l.actor_id, l.counterparty_id,',
    'SELECT l.collection_id, l.seq, l.event_type, l.actor_id, l.actor_member_id, l.counterparty_id,'
)

code = code.replace(
    'keys = ["collection_id", "seq", "event_type", "actor_id", "counterparty_id",',
    'keys = ["collection_id", "seq", "event_type", "actor_id", "actor_member_id", "counterparty_id",'
)

code = code.replace(
    'row[:11]',
    'row[:12]'
)


with open("src/snailstamp/apps/ledger/services.py", "w") as f:
    f.write(code)
print("Done patching services!")
