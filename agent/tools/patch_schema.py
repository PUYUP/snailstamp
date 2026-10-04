import re

with open("src/snailstamp/apps/ledger/sql/0001_schema.sql", "r") as f:
    sql = f.read()

# 0. Add state_snapshot column to ledger_logs (before other patches)
sql = sql.replace(
    'payload         jsonb,                  -- metadata kecil bebas (maks 4 KB). JANGAN isi data pribadi\n    PRIMARY KEY (collection_id, seq)',
    'payload         jsonb,                  -- metadata kecil bebas (maks 4 KB). JANGAN isi data pribadi\n    state_snapshot  jsonb,                  -- JSON snapshot Collection state saat log dibuat (untuk rekonstruksi)\n    PRIMARY KEY (collection_id, seq)'
)

# 1. Update id types from bigint to uuid
sql = re.sub(r'association_id\s+bigint', 'association_id uuid', sql)
sql = re.sub(r'issuer_id\s+bigint', 'issuer_id uuid', sql)
sql = re.sub(r'actor_id\s+bigint', 'actor_id uuid', sql)
sql = re.sub(r'counterparty_id\s+bigint', 'counterparty_id uuid', sql)
sql = re.sub(r'owner_id\s+bigint', 'owner_id uuid', sql)
sql = re.sub(r'claimed_by_id\s+bigint', 'claimed_by_id uuid', sql)

sql = re.sub(r'p_issuer bigint', 'p_issuer uuid', sql)
sql = re.sub(r'p_actor bigint', 'p_actor uuid', sql)
sql = re.sub(r'p_counterparty bigint', 'p_counterparty uuid', sql)
sql = re.sub(r'p_target bigint', 'p_target bigint', sql) # unchanged
sql = re.sub(r'v_owner bigint', 'v_owner uuid', sql)

# 2. Add member columns to tables
sql = sql.replace(
    'issuer_id     uuid      NOT NULL,\n',
    'issuer_id     uuid      NOT NULL,\n    issuer_member_id uuid      NOT NULL,\n'
)

sql = sql.replace(
    'actor_id        uuid      NOT NULL,   -- pelaku\n',
    'actor_id        uuid      NOT NULL,   -- pelaku\n    actor_member_id uuid      NOT NULL,\n'
)

sql = sql.replace(
    'from_association_id    uuid      NOT NULL,\n',
    'from_association_id    uuid      NOT NULL,\n    from_member_id  uuid        NOT NULL,\n'
)

sql = sql.replace(
    'claimed_by_id   uuid,                       -- NULL sampai diklaim\n',
    'claimed_by_id   uuid,                       -- NULL sampai diklaim\n    claimed_by_member_id uuid,\n'
)

sql = sql.replace(
    'CHECK ((claimed_by_id IS NULL) = (claimed_at IS NULL))',
    'CHECK ((claimed_by_id IS NULL) = (claimed_at IS NULL) AND (claimed_by_member_id IS NULL) = (claimed_by_id IS NULL))'
)

# 3. Update ledger_entry_hash
sql = sql.replace(
    'p_issuer uuid, p_reason text,',
    'p_issuer uuid, p_issuer_member uuid, p_reason text,'
)
sql = sql.replace(
    "p_id::text || '|' || p_issuer::text || '|' || p_reason || '|' ||",
    "p_id::text || '|' || p_issuer::text || '|' || p_issuer_member::text || '|' || p_reason || '|' ||"
)

# 4. Update ledger_log_hash
sql = sql.replace(
    'p_event smallint, p_actor uuid, p_counterparty uuid,',
    'p_event smallint, p_actor uuid, p_actor_member uuid, p_counterparty uuid,'
)
sql = sql.replace(
    "p_seq::text || '|' || p_event::text || '|' ||\n       p_actor::text || '|' || coalesce(p_counterparty::text, '') || '|' ||",
    "p_seq::text || '|' || p_event::text || '|' ||\n       p_actor::text || '|' || p_actor_member::text || '|' || coalesce(p_counterparty::text, '') || '|' ||"
)

# 5. Update triggers
sql = sql.replace(
    'OLD.from_association_id, OLD.created_at)',
    'OLD.from_association_id, OLD.from_member_id, OLD.created_at)'
)
sql = sql.replace(
    'NEW.from_association_id, NEW.created_at)',
    'NEW.from_association_id, NEW.from_member_id, NEW.created_at)'
)

sql = sql.replace(
    'OLD.id, OLD.issuer_id, OLD.reason',
    'OLD.id, OLD.issuer_id, OLD.issuer_member_id, OLD.reason'
)
sql = sql.replace(
    'NEW.id, NEW.issuer_id, NEW.reason',
    'NEW.id, NEW.issuer_id, NEW.issuer_member_id, NEW.reason'
)

# 5.5. Add ledger_collection_snapshot function (after triggers, before ledger_write_log)
sql = sql.replace(
    'END $$;\n\n-- internal: tulis satu log + hitung hash chain. Dipanggil di bawah row-lock collection.',
    'END $$;\n\n-- internal: generate JSON snapshot dari Collection state (untuk rekonstruksi)\nCREATE FUNCTION ledger_collection_snapshot(c ledger_collections) RETURNS jsonb\nLANGUAGE sql STABLE AS $$\nSELECT jsonb_build_object(\n  \'id\', c.id,\n  \'entry_id\', c.entry_id,\n  \'serial_no\', c.serial_no,\n  \'owner_id\', c.owner_id,\n  \'holder_id\', c.holder_id,\n  \'state\', c.state,\n  \'kind\', c.kind,\n  \'last_seq\', c.last_seq,\n  \'tool_uses\', c.tool_uses,\n  \'target_acts\', c.target_acts,\n  \'last_hash\', encode(c.last_hash, \'hex\'),\n  \'created_at\', c.created_at,\n  \'updated_at\', c.updated_at\n) $$;\n\n-- internal: tulis satu log + hitung hash chain. Dipanggil di bawah row-lock collection.'
)

# 6. Update ledger_write_log signature
sql = sql.replace(
    'c ledger_collections, p_event smallint, p_actor uuid, p_actor_member uuid,\n                                 p_counterparty uuid, p_payload jsonb, p_ts timestamptz,\n                                 p_target_id bigint DEFAULT NULL, p_target_seq int DEFAULT NULL,\n                                 p_action_id smallint DEFAULT NULL, p_content_hash bytea DEFAULT NULL,\n                                 OUT new_seq int, OUT new_hash bytea)',
    'c ledger_collections, p_event smallint, p_actor uuid, p_actor_member uuid,\n                                 p_counterparty uuid, p_payload jsonb, p_ts timestamptz,\n                                 p_target_id bigint DEFAULT NULL, p_target_seq int DEFAULT NULL,\n                                 p_action_id smallint DEFAULT NULL, p_content_hash bytea DEFAULT NULL,\n                                 p_state_snapshot jsonb DEFAULT NULL,\n                                 OUT new_seq int, OUT new_hash bytea)'
)
sql = sql.replace(
    'INSERT INTO ledger_logs (collection_id, actor_id, actor_member_id, counterparty_id, target_id, created_at,\n                           seq, target_seq, event_type, action_id, hash, content_hash, payload, state_snapshot)\n  VALUES (c.id, p_actor, p_actor_member, p_counterparty, p_target_id, p_ts, new_seq, p_target_seq,\n          p_event, p_action_id, new_hash, p_content_hash, p_payload, p_state_snapshot);',
    'INSERT INTO ledger_logs (collection_id, actor_id, actor_member_id, counterparty_id, target_id, created_at,\n                           seq, target_seq, event_type, action_id, hash, content_hash, payload, state_snapshot)\n  VALUES (c.id, p_actor, p_actor_member, p_counterparty, p_target_id, p_ts, new_seq, p_target_seq,\n          p_event, p_action_id, new_hash, p_content_hash, p_payload, p_state_snapshot);'
)

# 7. Update ledger_create_entry
sql = sql.replace(
    'p_issuer uuid, p_reason text,',
    'p_issuer uuid, p_issuer_member uuid, p_reason text,'
)
sql = sql.replace(
    'INSERT INTO ledger_entries (id, issuer_id, reason, supply, kind, metadata, content_hash, created_at)\n  VALUES (v_id, p_issuer, p_reason, p_supply, p_kind, p_metadata,\n          ledger_entry_hash(v_id, p_issuer, p_reason, p_supply, p_kind, p_metadata, v_now), v_now);',
    'INSERT INTO ledger_entries (id, issuer_id, issuer_member_id, reason, supply, kind, metadata, content_hash, created_at)\n  VALUES (v_id, p_issuer, p_issuer_member, p_reason, p_supply, p_kind, p_metadata,\n          ledger_entry_hash(v_id, p_issuer, p_issuer_member, p_reason, p_supply, p_kind, p_metadata, v_now), v_now);'
)

# 8. Update ledger_mint_batch
sql = sql.replace(
    'ledger_mint_batch(p_entry_id bigint, p_issuer uuid, p_batch int)',
    'ledger_mint_batch(p_entry_id bigint, p_issuer uuid, p_issuer_member uuid, p_batch int)'
)
sql = sql.replace(
    'INSERT INTO ledger_logs (collection_id, actor_id, counterparty_id, created_at,\n                             seq, event_type, hash, payload)\n    SELECT b.id, p_issuer, NULL, v_now, 1, 1::smallint,\n           ledger_log_hash(b.genesis, b.id, 1, 1::smallint, p_issuer, NULL, v_now, NULL), NULL',
    'INSERT INTO ledger_logs (collection_id, actor_id, actor_member_id, counterparty_id, created_at,\n                             seq, event_type, hash, payload)\n    SELECT b.id, p_issuer, p_issuer_member, NULL, v_now, 1, 1::smallint,\n           ledger_log_hash(b.genesis, b.id, 1, 1::smallint, p_issuer, p_issuer_member, NULL, v_now, NULL), NULL'
)

# 9. Update ledger_send
sql = sql.replace(
    'ledger_send(p_collection_id bigint, p_actor uuid,',
    'ledger_send(p_collection_id bigint, p_actor uuid, p_actor_member uuid,'
)
sql = sql.replace(
    'SELECT * INTO w FROM ledger_write_log(c, 2::smallint, p_actor, NULL, p_payload, v_now);',
    'SELECT * INTO w FROM ledger_write_log(c, 2::smallint, p_actor, p_actor_member, NULL, p_payload, v_now);'
)
sql = sql.replace(
    'INSERT INTO ledger_transfer_tokens (token, collection_id, from_association_id, created_at)\n  VALUES (transfer_token, c.id, p_actor, v_now);',
    'INSERT INTO ledger_transfer_tokens (token, collection_id, from_association_id, from_member_id, created_at)\n  VALUES (transfer_token, c.id, p_actor, p_actor_member, v_now);'
)

# 10. Update ledger_claim_transfer
sql = sql.replace(
    'ledger_claim_transfer(p_token uuid, p_actor uuid)',
    'ledger_claim_transfer(p_token uuid, p_actor uuid, p_actor_member uuid)'
)
sql = sql.replace(
    'SELECT * INTO w FROM ledger_write_log(c, 3::smallint, p_actor, c.owner_id, NULL, v_now);',
    'SELECT * INTO w FROM ledger_write_log(c, 3::smallint, p_actor, p_actor_member, c.owner_id, NULL, v_now);'
)
sql = sql.replace(
    'UPDATE ledger_transfer_tokens\n     SET claimed_by_id = p_actor, claimed_at = v_now\n   WHERE token = p_token;',
    'UPDATE ledger_transfer_tokens\n     SET claimed_by_id = p_actor, claimed_by_member_id = p_actor_member, claimed_at = v_now\n   WHERE token = p_token;'
)

# 11. Update ledger_cancel_send
sql = sql.replace(
    'ledger_cancel_send(p_collection_id bigint, p_actor uuid)',
    'ledger_cancel_send(p_collection_id bigint, p_actor uuid, p_actor_member uuid)'
)
sql = sql.replace(
    'SELECT * INTO w FROM ledger_write_log(c, 5::smallint, p_actor, NULL, NULL, v_now);',
    'SELECT * INTO w FROM ledger_write_log(c, 5::smallint, p_actor, p_actor_member, NULL, NULL, v_now);'
)

# 12. Update ledger_use
sql = sql.replace(
    'ledger_use(p_collection_id bigint, p_actor uuid,',
    'ledger_use(p_collection_id bigint, p_actor uuid, p_actor_member uuid,'
)
sql = sql.replace(
    'SELECT * INTO w FROM ledger_write_log(c, 4::smallint, p_actor, NULL, p_payload, v_now,\n                                        NULL, NULL, p_action, NULL);',
    'SELECT * INTO w FROM ledger_write_log(c, 4::smallint, p_actor, p_actor_member, NULL, p_payload, v_now,\n                                        NULL, NULL, p_action, NULL);'
)

# 13. Update ledger_act
sql = sql.replace(
    'p_action smallint, p_tool bigint, p_target bigint, p_actor uuid,',
    'p_action smallint, p_tool bigint, p_target bigint, p_actor uuid, p_actor_member uuid,'
)
sql = sql.replace(
    'SELECT * INTO wt FROM ledger_write_log(tool, 4::smallint, p_actor, NULL, NULL, v_now,\n                                         tgt.id, tgt.last_seq + 1, p_action, NULL);',
    'SELECT * INTO wt FROM ledger_write_log(tool, 4::smallint, p_actor, p_actor_member, NULL, NULL, v_now,\n                                         tgt.id, tgt.last_seq + 1, p_action, NULL);'
)
sql = sql.replace(
    'SELECT * INTO wg FROM ledger_write_log(tgt, 7::smallint, p_actor, NULL, p_payload, v_now,\n                                         tool.id, tool.last_seq + 1, p_action, p_content_hash);',
    'SELECT * INTO wg FROM ledger_write_log(tgt, 7::smallint, p_actor, p_actor_member, NULL, p_payload, v_now,\n                                         tool.id, tool.last_seq + 1, p_action, p_content_hash);'
)

# 14. Verify chain updates
sql = sql.replace(
    'ledger_entry_hash(e.id, e.issuer_id, e.reason',
    'ledger_entry_hash(e.id, e.issuer_id, e.issuer_member_id, e.reason'
)
sql = sql.replace(
    'r.event_type, r.actor_id,\n                                 r.counterparty_id, r.created_at, r.payload,',
    'r.event_type, r.actor_id, r.actor_member_id,\n                                 r.counterparty_id, r.created_at, r.payload,'
)

# 15. Execute GRANTS
sql = sql.replace(
    'ledger_write_log(ledger_collections, smallint, uuid, uuid, jsonb,',
    'ledger_write_log(ledger_collections, smallint, uuid, uuid, uuid, jsonb,'
)
sql = sql.replace(
    'ledger_create_entry(uuid, text, int, jsonb, smallint)',
    'ledger_create_entry(uuid, uuid, text, int, jsonb, smallint)'
)
sql = sql.replace(
    'ledger_mint_batch(bigint, uuid, int)',
    'ledger_mint_batch(bigint, uuid, uuid, int)'
)
sql = sql.replace(
    'ledger_send(bigint, uuid, jsonb)',
    'ledger_send(bigint, uuid, uuid, jsonb)'
)
sql = sql.replace(
    'ledger_claim_transfer(uuid, uuid)',
    'ledger_claim_transfer(uuid, uuid, uuid)'
)
sql = sql.replace(
    'ledger_cancel_send(bigint, uuid)',
    'ledger_cancel_send(bigint, uuid, uuid)'
)
sql = sql.replace(
    'ledger_use(bigint, uuid, smallint, jsonb)',
    'ledger_use(bigint, uuid, uuid, smallint, jsonb)'
)
sql = sql.replace(
    'ledger_act(smallint, bigint, bigint, uuid, bytea, jsonb)',
    'ledger_act(smallint, bigint, bigint, uuid, uuid, bytea, jsonb)'
)

# 16. Ensure BRIN / INDEX also matches. In _LEAVES_SQL verify_proof it accesses these
sql = sql.replace(
    "SELECT l.collection_id, l.seq, l.event_type, l.actor_id, l.counterparty_id, ",
    "SELECT l.collection_id, l.seq, l.event_type, l.actor_id, l.actor_member_id, l.counterparty_id, "
)

# 17. Add state_snapshot to all ledger_write_log calls
sql = sql.replace(
    'SELECT * INTO w FROM ledger_write_log(c, 2::smallint, p_actor, p_actor_member, NULL, p_payload, v_now);',
    'SELECT * INTO w FROM ledger_write_log(c, 2::smallint, p_actor, p_actor_member, NULL, p_payload, v_now, NULL, NULL, NULL, NULL, ledger_collection_snapshot(c));'
)
sql = sql.replace(
    'SELECT * INTO w FROM ledger_write_log(c, 3::smallint, p_actor, p_actor_member, c.owner_id, NULL, v_now);',
    'SELECT * INTO w FROM ledger_write_log(c, 3::smallint, p_actor, p_actor_member, c.owner_id, NULL, v_now, NULL, NULL, NULL, NULL, ledger_collection_snapshot(c));'
)
sql = sql.replace(
    'SELECT * INTO w FROM ledger_write_log(c, 5::smallint, p_actor, p_actor_member, NULL, NULL, v_now);',
    'SELECT * INTO w FROM ledger_write_log(c, 5::smallint, p_actor, p_actor_member, NULL, NULL, v_now, NULL, NULL, NULL, NULL, ledger_collection_snapshot(c));'
)
sql = sql.replace(
    'SELECT * INTO w FROM ledger_write_log(c, 4::smallint, p_actor, p_actor_member, NULL, p_payload, v_now,\n                                        NULL, NULL, p_action, NULL);',
    'SELECT * INTO w FROM ledger_write_log(c, 4::smallint, p_actor, p_actor_member, NULL, p_payload, v_now,\n                                        NULL, NULL, p_action, NULL, ledger_collection_snapshot(c));'
)
sql = sql.replace(
    'SELECT * INTO wt FROM ledger_write_log(tool, 4::smallint, p_actor, p_actor_member, NULL, NULL, v_now,\n                                         tgt.id, tgt.last_seq + 1, p_action, NULL);',
    'SELECT * INTO wt FROM ledger_write_log(tool, 4::smallint, p_actor, p_actor_member, NULL, NULL, v_now,\n                                         tgt.id, tgt.last_seq + 1, p_action, NULL, ledger_collection_snapshot(tool));'
)
sql = sql.replace(
    'SELECT * INTO wg FROM ledger_write_log(tgt, 7::smallint, p_actor, p_actor_member, NULL, p_payload, v_now,\n                                         tool.id, tool.last_seq + 1, p_action, p_content_hash);',
    'SELECT * INTO wg FROM ledger_write_log(tgt, 7::smallint, p_actor, p_actor_member, NULL, p_payload, v_now,\n                                         tool.id, tool.last_seq + 1, p_action, p_content_hash, ledger_collection_snapshot(tgt));'
)
sql = sql.replace(
    'INSERT INTO ledger_logs (collection_id, actor_id, actor_member_id, counterparty_id, created_at,\n                             seq, event_type, hash, payload)\n    SELECT b.id, p_issuer, p_issuer_member, NULL, v_now, 1, 1::smallint,\n           ledger_log_hash(b.genesis, b.id, 1, 1::smallint, p_issuer, p_issuer_member, NULL, v_now, NULL), NULL',
    'INSERT INTO ledger_logs (collection_id, actor_id, actor_member_id, counterparty_id, created_at,\n                             seq, event_type, hash, payload, state_snapshot)\n    SELECT b.id, p_issuer, p_issuer_member, NULL, v_now, 1, 1::smallint,\n           ledger_log_hash(b.genesis, b.id, 1, 1::smallint, p_issuer, p_issuer_member, NULL, v_now, NULL), NULL, ledger_collection_snapshot(c)'
)

with open("src/snailstamp/apps/ledger/sql/0001_schema.sql", "w") as f:
    f.write(sql)
print("Done patching schema!")
