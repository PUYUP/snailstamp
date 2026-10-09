-- Contoh registry hobi analog. Ubah/tambah sesuka Anda.
-- PENTING: id masuk ke hash log -> ID TETAP, jangan diubah atau dipakai ulang.
-- Idempotent (aman dijalankan ulang).

INSERT INTO ledger_kinds (id, code, label, max_as_tool, max_as_target, restricted) VALUES
  (1, 'pen',        'pena',                   NULL, NULL, false),
  (2, 'journal',    'jurnal',                 NULL, NULL, false),
  (3, 'letter',     'surat pos',              NULL, NULL, false),
  (4, 'stamp',      'perangko',                  1, NULL, false),  -- sekali tempel lalu habis
  (5, 'postmarker', 'cap pos',                NULL, NULL, true),   -- HANYA penerbit resmi (ledger_kind_issuers)
  (6, 'camera',     'kamera film',            NULL, NULL, false),
  (7, 'film_roll',  'rol film',               NULL,   36, false),  -- 36 bingkai
  (8, 'turntable',  'pemutar piringan hitam', NULL, NULL, false),
  (9, 'vinyl',      'piringan hitam',         NULL, NULL, false)
ON CONFLICT (id) DO NOTHING;
-- Daftarkan penerbit resmi cap pos (association id kantor pos Anda), dari migrasi/admin:
--   INSERT INTO ledger_kind_issuers (kind_id, association_id) VALUES (5, <id_kantor_pos>);

INSERT INTO ledger_actions (id, code, label) VALUES
  (1, 'write',    'menulis'),
  (2, 'affix',    'menempelkan'),
  (3, 'postmark', 'mengecap pos'),
  (4, 'expose',   'memotret'),
  (5, 'play',     'memutar'),
  (6, 'read',     'membaca'),          -- aksi tunggal: dipakai lewat ledger_use
  (7, 'revise',   'revisi log')        -- aksi kompensasi / koreksi misalnya penggunaan "Tipe-X" (penghapus)
ON CONFLICT (id) DO NOTHING;

-- target_access: 1 pemilik sasaran | 2 siapa pun saat sasaran DIKIRIM | 3 siapa pun, sasaran aktif
INSERT INTO ledger_action_rules (action_id, tool_kind, target_kind, target_access) VALUES
  (1, 1, 2, 1),   -- pena   menulis di jurnal
  (1, 1, 3, 1),   -- pena   menulis di surat
  (2, 4, 3, 1),   -- perangko ditempel ke surat
  (3, 5, 3, 2),   -- cap pos mengecap surat YANG SEDANG DIKIRIM (oleh pemegang cap, mis. kantor pos)
  (4, 6, 7, 1),   -- kamera memotret rol film
  (5, 8, 9, 1),   -- pemutar memutar piringan hitam
  (7, 1, 2, 1)    -- alat (misal pena/sistem) merevisi log pada jurnal milik sendiri
ON CONFLICT (action_id, tool_kind, target_kind) DO NOTHING;
