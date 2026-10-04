"""
Tes ledger (butuh PostgreSQL; skema dibuat oleh migrasi 0001/0002).

    python manage.py test snailstamp.apps.ledger

Tes tidak memakai model tenant: ledger tidak punya FK ke sana, dan pengecekan keanggotaan
diganti fungsi palsu di bawah lewat settings.LEDGER_MEMBER_CHECK.
"""
import hashlib
import time
import uuid
from datetime import timedelta

from django.db import DatabaseError, connection, transaction
from django.test import SimpleTestCase, TestCase, override_settings

from . import services as svc
from .models import Collection, Entry, Holding, Log, LedgerWriteForbidden

_MEMBERS = {}          # association_id -> {member_id, ...}


def fake_member_check(association_id, member_id):
    return member_id in _MEMBERS.get(association_id, ())


def _is_superuser():
    with connection.cursor() as cur:
        cur.execute("SELECT rolsuper FROM pg_roles WHERE rolname = current_user")
        return cur.fetchone()[0]


@override_settings(LEDGER_MEMBER_CHECK=f"{__name__}.fake_member_check")
class LedgerTestCase(TestCase):
    def setUp(self):
        _MEMBERS.clear()
        self.fam_a, self.fam_b = uuid.uuid4(), uuid.uuid4()
        self.ayah, self.kakak, self.mem_b = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
        _MEMBERS[self.fam_a] = {self.ayah, self.kakak}
        _MEMBERS[self.fam_b] = {self.mem_b}
        # pena + jurnal milik keluarga A, dibuat oleh ayah
        _, (self.pena,) = svc.create_item(self.fam_a, self.ayah, "pena warisan", 1, "pen")
        _, (self.jurnal,) = svc.create_item(self.fam_a, self.ayah, "jurnal keluarga", 1, "journal")

    def require_superuser(self):
        """Dicek saat tes berjalan (DB tes sudah ada), bukan saat modul diimpor."""
        if not _is_superuser():
            self.skipTest("perlu superuser untuk melewati trigger")

    def _actors(self, collection_id):
        return [(str(l.actor_id), str(l.actor_member_id)) for l in svc.item_history(collection_id)]


class FamilyScenarioTests(LedgerTestCase):
    def test_inheritance_without_breaking_history(self):
        a, ayah, kakak = str(self.fam_a), str(self.ayah), str(self.kakak)
        svc.act("write", self.pena, self.jurnal, self.fam_a, self.ayah, content="tulisan ayah")
        # ayah meninggal; kakak (member lain, association SAMA) melanjutkan tanpa mengubah apa pun
        svc.act("write", self.pena, self.jurnal, self.fam_a, self.kakak, content="tulisan kakak")

        self.assertEqual(self._actors(self.jurnal), [(a, ayah), (a, ayah), (a, kakak)])
        self.assertEqual(Collection.objects.get(pk=self.jurnal).owner_id, self.fam_a)
        for c in (self.pena, self.jurnal):
            self.assertEqual(svc.verify_chain(c), (True, None, "ok"))

        # pewarisan ke association lain: send/claim biasa
        _, token = svc.send(self.jurnal, self.fam_a, self.kakak)
        svc.claim_transfer(token, self.fam_b, self.mem_b)
        self.assertEqual(Collection.objects.get(pk=self.jurnal).owner_id, self.fam_b)
        self.assertEqual(svc.verify_chain(self.jurnal), (True, None, "ok"))
        last = list(svc.item_history(self.jurnal))[-1]
        self.assertEqual((str(last.actor_id), str(last.actor_member_id), str(last.counterparty_id)),
                         (str(self.fam_b), str(self.mem_b), a))
        # holdings pindah ke association, bukan ke orang
        self.assertFalse(Holding.objects.filter(owner_id=self.fam_a, collection_id=self.jurnal).exists())
        self.assertTrue(Holding.objects.filter(owner_id=self.fam_b, collection_id=self.jurnal).exists())

    def test_creator_is_recorded_for_entry_and_collection(self):
        entry = Entry.objects.get(pk=Collection.objects.get(pk=self.jurnal).entry_id)
        self.assertEqual((entry.issuer_id, entry.issuer_member_id), (self.fam_a, self.ayah))
        mint = svc.item_creator(self.jurnal)
        self.assertEqual((mint.event_type, mint.actor_id, mint.actor_member_id),
                         (Log.Event.MINT, self.fam_a, self.ayah))

    def test_restricted_kind_needs_official_issuer_association(self):
        with self.assertRaises(svc.Forbidden), transaction.atomic():
            svc.create_item(self.fam_b, self.mem_b, "cap pos palsu", 1, "postmarker")
        with connection.cursor() as cur:   # didaftarkan lewat migrasi/admin oleh pemilik skema
            cur.execute("INSERT INTO ledger_kind_issuers (kind_id, association_id) VALUES (5, %s)", [self.fam_b])
        _, (cap,) = svc.create_item(self.fam_b, self.mem_b, "cap pos resmi", 1, "postmarker")
        self.assertTrue(cap)


class AuthorizationTests(LedgerTestCase):
    def test_member_of_other_association_is_refused_and_nothing_written(self):
        before = Log.objects.filter(collection_id=self.pena).count()
        with self.assertRaises(svc.Forbidden):
            svc.use(self.pena, self.fam_a, self.mem_b, "read")        # mem_b milik fam_b
        with self.assertRaises(svc.Forbidden):
            svc.create_entry(self.fam_a, self.mem_b, "menyusup", 1)
        self.assertEqual(Log.objects.filter(collection_id=self.pena).count(), before)

    def test_non_owner_association_is_refused_by_database(self):
        with self.assertRaises(svc.Forbidden), transaction.atomic():
            svc.use(self.pena, self.fam_b, self.mem_b)               # member sah, tapi bukan pemilik

    def test_missing_association_or_member_is_invalid_input(self):
        with self.assertRaises(svc.InvalidInput):
            svc.use(self.pena, self.fam_a, None)
        with self.assertRaises(svc.InvalidInput):                     # lapis DB, lewati lapis Python
            with transaction.atomic():
                svc._call("SELECT ledger_use(%s, %s, NULL, NULL::smallint, NULL::jsonb)", [self.pena, self.fam_a])

    def test_unknown_kind_and_action(self):
        with self.assertRaises(svc.InvalidInput):
            svc.create_item(self.fam_a, self.ayah, "x", 1, "tidak-ada")
        with self.assertRaises(svc.InvalidInput):
            svc.use(self.pena, self.fam_a, self.ayah, "tidak-ada")


class ImmutabilityTests(LedgerTestCase):
    def test_orm_writes_are_forbidden(self):
        with self.assertRaises(LedgerWriteForbidden):
            Entry.objects.create()
        with self.assertRaises(LedgerWriteForbidden):
            Collection.objects.filter(pk=self.pena).update(state=2)

    def test_sql_update_and_delete_of_logs_are_refused(self):
        for sql in ("UPDATE ledger_logs SET actor_member_id = gen_random_uuid()", "DELETE FROM ledger_logs"):
            with self.assertRaises(DatabaseError), transaction.atomic(), connection.cursor() as cur:
                cur.execute(sql)

    def test_rewriting_the_member_in_history_is_detected(self):
        """Superuser DB bisa menimpa log (melewati trigger), tapi chain langsung rusak."""
        self.require_superuser()
        svc.act("write", self.pena, self.jurnal, self.fam_a, self.ayah, content="asli")
        self.assertEqual(svc.verify_chain(self.jurnal), (True, None, "ok"))
        with connection.cursor() as cur:
            cur.execute("SET LOCAL session_replication_role = replica")
            cur.execute("UPDATE ledger_logs SET actor_member_id = %s WHERE collection_id = %s AND seq = 2",
                        [self.kakak, self.jurnal])
            cur.execute("SET LOCAL session_replication_role = DEFAULT")
        valid, seq, detail = svc.verify_chain(self.jurnal)
        self.assertEqual((valid, seq), (False, 2))
        self.assertIn("hash log", detail)


class SealerAndProofTests(LedgerTestCase):
    def test_blocks_and_proofs_cover_member_data(self):
        svc.act("write", self.pena, self.jurnal, self.fam_a, self.ayah, content="catatan")
        time.sleep(2.2)                                              # lewati jendela 1 dtk
        while svc.seal_next_block(window=timedelta(seconds=1), safety_lag=timedelta(0)):
            pass
        self.assertEqual(svc.verify_blocks(), (True, None))
        for coll, seq in ((self.jurnal, 1), (self.jurnal, 2), (self.pena, 2)):
            proof = svc.build_proof(coll, seq)
            self.assertIsNotNone(proof, (coll, seq))
            self.assertTrue(svc.verify_proof(proof))
        proof = svc.build_proof(self.jurnal, 2)
        self.assertEqual(proof["log"]["actor_member_id"], self.ayah)
        proof["log"]["actor_member_id"] = self.kakak                 # ganti pelaku -> bukti batal
        self.assertFalse(svc.verify_proof(proof))
        self.assertTrue(svc.verify_content(self.jurnal, 2, "catatan"))
        self.assertFalse(svc.verify_content(self.jurnal, 2, "diubah"))


class MerkleTests(SimpleTestCase):
    def test_streaming_root_equals_path_root_for_all_sizes(self):
        for n in range(1, 70):
            leaves = [hashlib.sha256(str(i).encode()).digest() for i in range(n)]
            root, count = svc.merkle_root(iter(leaves))
            self.assertEqual(count, n)
            for idx in {0, n // 2, n - 1}:
                node = hashlib.sha256(b"\x00" + leaves[idx]).digest()
                for side, sib in svc._merkle_path(leaves, idx):
                    sib = bytes.fromhex(sib)
                    node = hashlib.sha256(b"\x01" + (sib + node if side == "L" else node + sib)).digest()
                self.assertEqual(node, root, (n, idx))


class HolderAssignTests(LedgerTestCase):
    """
    Analogi keluarga + motor:
      - fam_a = keluarga
      - ayah, kakak = member keluarga
      - pena = motor milik keluarga
    """

    def test_holder_is_set_to_creator_on_mint(self):
        """Saat item lahir (MINT), holder langsung diisi dengan member pembuat."""
        col = Collection.objects.get(pk=self.pena)
        self.assertEqual(col.holder_id, self.ayah)

    def test_assign_writes_event_to_chain(self):
        """assign() harus menulis log ASSIGN (type 6) ke hash chain."""
        before_seq = Collection.objects.get(pk=self.pena).last_seq
        svc.assign(self.pena, self.fam_a, self.ayah, self.kakak)
        after_seq = Collection.objects.get(pk=self.pena).last_seq
        self.assertEqual(after_seq, before_seq + 1)

        log = Log.objects.get(pk=(self.pena, after_seq))
        self.assertEqual(log.event_type, Log.Event.ASSIGN)
        self.assertEqual(log.actor_id, self.fam_a)
        self.assertEqual(log.actor_member_id, self.ayah)

    def test_assign_updates_holder_field(self):
        """Setelah assign(), kolom holder_id di Collection harus berubah."""
        svc.assign(self.pena, self.fam_a, self.ayah, self.kakak)
        col = Collection.objects.get(pk=self.pena)
        self.assertEqual(col.holder_id, self.kakak)

    def test_assign_payload_contains_prev_and_new_holder(self):
        """Payload log ASSIGN harus merekam prev_holder dan new_holder."""
        svc.assign(self.pena, self.fam_a, self.ayah, self.kakak)
        seq = Collection.objects.get(pk=self.pena).last_seq
        log = Log.objects.get(pk=(self.pena, seq))
        self.assertEqual(log.payload["new_holder"], str(self.kakak))
        self.assertEqual(log.payload["prev_holder"], str(self.ayah))

    def test_assign_to_none_releases_holder(self):
        """assign(new_holder=None) melepas pemegang -> holder_id menjadi NULL."""
        svc.assign(self.pena, self.fam_a, self.ayah, None)
        col = Collection.objects.get(pk=self.pena)
        self.assertIsNone(col.holder_id)
        # Masih ada di chain
        seq = col.last_seq
        log = Log.objects.get(pk=(self.pena, seq))
        self.assertEqual(log.event_type, Log.Event.ASSIGN)
        self.assertIsNone(log.payload["new_holder"])

    def test_assign_chain_stays_valid(self):
        """Hash chain tetap valid setelah beberapa kali assign()."""
        svc.assign(self.pena, self.fam_a, self.ayah, self.kakak)
        svc.assign(self.pena, self.fam_a, self.kakak, self.ayah)
        svc.assign(self.pena, self.fam_a, self.ayah, None)
        self.assertEqual(svc.verify_chain(self.pena), (True, None, "ok"))

    def test_item_assignments_returns_full_history(self):
        """item_assignments() mengembalikan semua event ASSIGN secara berurutan."""
        svc.assign(self.pena, self.fam_a, self.ayah, self.kakak)
        svc.assign(self.pena, self.fam_a, self.kakak, None)
        history = list(svc.item_assignments(self.pena))
        self.assertEqual(len(history), 2)
        self.assertEqual(history[0].payload["new_holder"], str(self.kakak))
        self.assertIsNone(history[1].payload["new_holder"])

    def test_claim_transfer_updates_holder_to_claiming_member(self):
        """Saat item diklaim via QR, holder langsung diisi member penerima."""
        _, token = svc.send(self.pena, self.fam_a, self.ayah)
        svc.claim_transfer(token, self.fam_b, self.mem_b)
        col = Collection.objects.get(pk=self.pena)
        self.assertEqual(col.owner_id, self.fam_b)
        self.assertEqual(col.holder_id, self.mem_b)

    def test_cancel_send_resets_holder_to_null(self):
        """Saat pengiriman dibatalkan, holder direset ke NULL karena tidak ada penerimaan."""
        _, token = svc.send(self.pena, self.fam_a, self.ayah)
        svc.cancel_send(self.pena, self.fam_a, self.ayah)
        col = Collection.objects.get(pk=self.pena)
        self.assertEqual(col.state, Collection.State.ACTIVE)
        self.assertIsNone(col.holder_id)

    def test_assign_by_non_owner_association_is_refused(self):
        """Association bukan pemilik tidak boleh assign holder."""
        with self.assertRaises(svc.Forbidden), transaction.atomic():
            svc.assign(self.pena, self.fam_b, self.mem_b, self.mem_b)

    def test_assign_while_in_transit_is_refused(self):
        """Tidak bisa assign holder saat item sedang dalam pengiriman."""
        svc.send(self.pena, self.fam_a, self.ayah)
        with self.assertRaises(svc.InvalidState), transaction.atomic():
            svc.assign(self.pena, self.fam_a, self.ayah, self.kakak)

    def test_assign_member_from_wrong_association_is_refused(self):
        """_check_member() menolak mem_b (dari fam_b) untuk bertindak atas fam_a."""
        with self.assertRaises(svc.Forbidden):
            svc.assign(self.pena, self.fam_a, self.mem_b, self.kakak)


class SerialNoTests(LedgerTestCase):
    def test_serial_no_has_prefix_and_eight_digits(self):
        """serial_no harus diawali prefix dan diikuti minimal 8 angka."""
        _, (col_id,) = svc.create_item(self.fam_a, self.ayah, "stiker", 1, "pen", prefix="RBZ-")
        col = Collection.objects.get(pk=col_id)
        self.assertTrue(col.serial_no.startswith("RBZ-"), col.serial_no)
        angka = col.serial_no[4:]
        self.assertTrue(angka.isdigit(), f"bukan angka: {angka}")
        self.assertGreaterEqual(len(angka), 8, f"kurang dari 8 digit: {angka}")

    def test_serial_no_is_unique_across_bulk_mint(self):
        """serial_no harus unik di seluruh batch mint (tanpa collision)."""
        _, col_ids = svc.create_item(self.fam_a, self.ayah, "batch stiker", 500, "pen", prefix="FAM-")
        serials = list(Collection.objects.filter(pk__in=col_ids).values_list("serial_no", flat=True))
        self.assertEqual(len(serials), len(set(serials)), "ada serial_no yang duplikat!")

    def test_serial_no_without_prefix(self):
        """Tanpa prefix, serial_no tetap berupa string 8+ digit."""
        _, (col_id,) = svc.create_item(self.fam_a, self.ayah, "item polos", 1, "pen")
        col = Collection.objects.get(pk=col_id)
        self.assertTrue(col.serial_no.isdigit())
        self.assertGreaterEqual(len(col.serial_no), 8)



class HolderReplayTests(LedgerTestCase):
    """verify_chain me-replay holder: ASSIGN dikenali, dan kolom holder_id tak bisa dipalsukan."""

    def test_holder_replay_across_mint_assign_send_receive(self):
        svc.assign(self.pena, self.fam_a, self.ayah, self.kakak)
        self.assertEqual(svc.verify_chain(self.pena), (True, None, "ok"))
        _, token = svc.send(self.pena, self.fam_a, self.kakak)
        svc.claim_transfer(token, self.fam_b, self.mem_b)           # penerima = pemegang baru
        svc.assign(self.pena, self.fam_b, self.mem_b, None)
        self.assertEqual(svc.verify_chain(self.pena), (True, None, "ok"))

    def test_cancel_send_holder_reset_is_replayable(self):
        _, token = svc.send(self.pena, self.fam_a, self.ayah)
        svc.cancel_send(self.pena, self.fam_a, self.ayah)
        self.assertIsNone(Collection.objects.get(pk=self.pena).holder_id)
        self.assertEqual(svc.verify_chain(self.pena), (True, None, "ok"))

    def test_forged_holder_column_is_detected(self):
        with connection.cursor() as cur:        # pemilik skema bisa UPDATE kolom non-beku
            cur.execute("UPDATE ledger_collections SET holder_id = %s WHERE id = %s", [self.kakak, self.pena])
        valid, seq, detail = svc.verify_chain(self.pena)
        self.assertFalse(valid)
        self.assertIn("holder", detail)

    def test_assign_to_member_of_another_association_is_refused(self):
        before = Collection.objects.get(pk=self.pena).last_seq
        with self.assertRaises(svc.Forbidden):
            svc.assign(self.pena, self.fam_a, self.ayah, self.mem_b)    # mem_b anggota fam_b
        self.assertEqual(Collection.objects.get(pk=self.pena).last_seq, before)

    def test_forged_assign_payload_is_detected(self):
        self.require_superuser()
        svc.assign(self.pena, self.fam_a, self.ayah, self.kakak)
        seq = Collection.objects.get(pk=self.pena).last_seq
        with connection.cursor() as cur:
            cur.execute("SET LOCAL session_replication_role = replica")
            cur.execute("UPDATE ledger_logs SET payload = jsonb_build_object('prev_holder', NULL, 'new_holder', %s::text) "
                        "WHERE collection_id = %s AND seq = %s", [str(self.mem_b), self.pena, seq])
            cur.execute("SET LOCAL session_replication_role = DEFAULT")
        valid, broken, _ = svc.verify_chain(self.pena)
        self.assertEqual((valid, broken), (False, seq))                   # hash log tidak cocok


class AttachContentTests(LedgerTestCase):
    def test_use_records_content_hash_and_verify_content_accepts_use_logs(self):
        seq = svc.use(self.jurnal, self.fam_a, self.ayah, "attach", payload={"media": "m1"}, content=b"foto-1")
        log = Log.objects.get(pk=(self.jurnal, seq))
        self.assertEqual((log.event_type, bytes(log.content_hash)), (Log.Event.USE, svc.content_sha256(b"foto-1")))
        self.assertTrue(svc.verify_content(self.jurnal, seq, b"foto-1"))
        self.assertFalse(svc.verify_content(self.jurnal, seq, b"foto-2"))
        self.assertTrue(svc.verify_content(self.jurnal, seq, content_hash=svc.content_sha256(b"foto-1")))
        self.assertEqual(svc.verify_chain(self.jurnal), (True, None, "ok"))

    def test_content_hash_must_be_sha256(self):
        with self.assertRaises(svc.InvalidInput), transaction.atomic():
            svc.use(self.jurnal, self.fam_a, self.ayah, "attach", content_hash=b"terlalu-pendek")

    def test_verify_content_needs_exactly_one_argument(self):
        with self.assertRaises(svc.InvalidInput):
            svc.verify_content(self.jurnal, 1)
        with self.assertRaises(svc.InvalidInput):
            svc.verify_content(self.jurnal, 1, b"x", svc.content_sha256(b"x"))

    def test_streaming_hash_equals_in_memory_hash(self):
        import io
        for size in (0, 1, 1023, 1024, 1025, 10_000):
            data = bytes(range(256)) * (size // 256) + bytes(range(size % 256))
            self.assertEqual(svc.sha256_stream(io.BytesIO(data), chunk_size=1024), svc.content_sha256(data))
            self.assertEqual(svc.sha256_chunks(data[i:i + 700] for i in range(0, len(data), 700)),
                             svc.content_sha256(data))
