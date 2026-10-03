import re

with open("src/snailstamp/apps/ledger/sql/0001_schema.sql", "r") as f:
    sql = f.read()

# 1. Update ledger_genesis_hash signature
sql = sql.replace(
    'ledger_genesis_hash(p_entry_hash bytea, p_collection_id bigint, p_serial int)',
    'ledger_genesis_hash(p_entry_hash bytea, p_collection_id bigint, p_serial varchar)'
)

# 2. Update ledger_collections.serial_no
sql = sql.replace(
    'serial_no     integer     NOT NULL CHECK (serial_no > 0),',
    'serial_no     varchar(100) NOT NULL,'
)

# 3. Update ledger_mint_batch signature and generation
sql = sql.replace(
    'ledger_mint_batch(p_entry_id bigint, p_issuer uuid, p_issuer_member uuid, p_batch int)',
    'ledger_mint_batch(p_entry_id bigint, p_issuer uuid, p_issuer_member uuid, p_batch int, p_prefix varchar DEFAULT \'\')'
)
sql = sql.replace(
    "FROM (SELECT nextval('ledger_collection_id_seq') AS id, s AS serial_no\n          FROM generate_series(v_from, v_to) s) x",
    "FROM (SELECT nextval('ledger_collection_id_seq') AS id, p_prefix || s::text AS serial_no\n          FROM generate_series(v_from, v_to) s) x"
)

# 4. Remove serial_no from frozen trigger update logic because it changed type and check logic?
# Actually trigger logic just compares it, which is fine regardless of type.

with open("src/snailstamp/apps/ledger/sql/0001_schema.sql", "w") as f:
    f.write(sql)
print("Done patching schema for serial_no!")
