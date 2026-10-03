import re

with open("src/snailstamp/apps/ledger/sql/0001_schema.sql", "r") as f:
    sql = f.read()

# Replace the generation logic in ledger_mint_batch
old_select = "FROM (SELECT nextval('ledger_collection_id_seq') AS id, p_prefix || s::text AS serial_no\n          FROM generate_series(v_from, v_to) s) x"
new_select = "FROM (SELECT nextval('ledger_collection_id_seq') AS id, \n                 p_prefix || ((((s::bigint * 38742041) + (p_entry_id * 1234567)) % 90000000) + 10000000)::text AS serial_no\n          FROM generate_series(v_from, v_to) s) x"

if old_select in sql:
    sql = sql.replace(old_select, new_select)
else:
    print("Could not find the old select statement in schema!")

with open("src/snailstamp/apps/ledger/sql/0001_schema.sql", "w") as f:
    f.write(sql)
print("Done patching schema for random serial_no!")
