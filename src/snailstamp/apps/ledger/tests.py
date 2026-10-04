"""
Tes ledger (butuh PostgreSQL; skema dibuat oleh migrasi 0001/0002).

    python manage.py test snailstamp.apps.ledger

Tes tidak memakai model tenant: ledger tidak punya FK ke sana, dan pengecekan keanggotaan
diganti fungsi palsu di bawah lewat settings.LEDGER_MEMBER_CHECK.
"""
import hashlib
import itertools
import json
import os
import threading
import time
import uuid
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import skipUnless

from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import DatabaseError, connection, transaction
from django.test import SimpleTestCase, TestCase, override_settings
from django.utils import timezone as dj_timezone
from nacl import signing as nacl_signing

from . import anchors, shamir
from . import services as svc
from .models import (
    BlockAnchor,
    BlockSignature,
    Collection,
    Entry,
    Holding,
    LedgerWriteForbidden,
    Log,
    SealerKey,
    SealerKeyAuditLog,
)

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

    @skipUnless(_is_superuser(), "perlu superuser untuk melewati trigger")
    def test_rewriting_the_member_in_history_is_detected(self):
        """Superuser DB bisa menimpa log (melewati trigger), tapi chain langsung rusak."""
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
        private, public = svc.generate_sealer_keypair()
        keys = [{"sealer_id": svc.register_sealer("sealer-1", public).id, "private_key": private}]
        while svc.seal_next_block(window=timedelta(seconds=1), safety_lag=timedelta(0), sealer_private_keys=keys):
            pass
        self.assertEqual(svc.verify_blocks(), (True, None))
        for coll, seq in ((self.jurnal, 1), (self.jurnal, 2), (self.pena, 2)):
            proof = svc.build_proof(coll, seq)
            self.assertIsNotNone(proof, (coll, seq))
            self.assertTrue(svc.verify_proof(proof))
        proof = svc.build_proof(self.jurnal, 2)
        self.assertEqual(proof["log"]["actor_member_id"], str(self.ayah))
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



# ---------------------------------------------------------------- sealer keys
Event = SealerKeyAuditLog.EventType


@override_settings(LEDGER_MEMBER_CHECK=f"{__name__}.fake_member_check")
class SealerKeyTestCase(TestCase):
    def setUp(self):
        _MEMBERS.clear()
        self.assoc, self.member = uuid.uuid4(), uuid.uuid4()
        _MEMBERS[self.assoc] = {self.member}

    def _sealer(self, name, region=""):
        private, public = svc.generate_sealer_keypair()
        return svc.register_sealer(name, public, region=region), private

    def _seal(self, keys=None):
        """Buat entry, tunggu jendela 1 dtk lewat, segel semua blok. Return blok terakhir."""
        svc.create_entry(self.assoc, self.member, "uji sealer", 1)
        time.sleep(2.2)
        last = None
        while block := svc.seal_next_block(window=timedelta(seconds=1), safety_lag=timedelta(0),
                                           sealer_private_keys=keys):
            last = block
        self.assertIsNotNone(last)
        return last


class SealerAuditLogTests(SealerKeyTestCase):
    def test_register_rotate_revoke_and_expire_are_audited(self):
        sealer, _ = self._sealer("sealer-1", region="ap-southeast-1")
        _, new_public = svc.generate_sealer_keypair()
        actor = {"user_id": "42", "email": "ops@example.com", "ip": "10.0.0.1"}
        new_key = svc.rotate_sealer_key(sealer.id, new_public, reason="compromised", actor_info=actor)

        rotated = SealerKeyAuditLog.objects.get(event_type=Event.KEY_ROTATED)
        self.assertEqual((rotated.old_key_id, rotated.new_key_id), (sealer.id, new_key.id))
        self.assertEqual((rotated.old_public_key, rotated.new_public_key), (sealer.public_key, new_public))
        self.assertEqual(rotated.rotation_reason, "compromised")
        self.assertEqual((rotated.actor_email, rotated.actor_ip), ("ops@example.com", "10.0.0.1"))
        self.assertEqual(rotated.region, "ap-southeast-1")
        sealer.refresh_from_db()
        self.assertEqual(sealer.status, SealerKey.KeyStatus.EXPIRED)
        self.assertIsNotNone(sealer.deactivated_at)
        self.assertEqual((new_key.previous_key_id, new_key.region), (sealer.id, "ap-southeast-1"))
        with self.assertRaises(svc.InvalidState):
            svc.rotate_sealer_key(sealer.id, svc.generate_sealer_keypair()[1])

        svc.revoke_sealer_key(new_key.id, reason="leaked")
        self.assertTrue(SealerKeyAuditLog.objects.filter(event_type=Event.KEY_REVOKED,
                                                         old_key_id=new_key.id,
                                                         rotation_reason="leaked").exists())

        old, _ = self._sealer("sealer-old")
        SealerKey.objects.filter(pk=old.pk).update(valid_until=dj_timezone.now() - timedelta(days=1))
        self.assertEqual(svc.expire_old_keys(), 1)
        self.assertTrue(SealerKeyAuditLog.objects.filter(event_type=Event.KEY_EXPIRED,
                                                         sealer_id=old.id).exists())
        trail = list(svc.sealer_audit_trail(sealer_name="sealer-1").values_list("event_type", flat=True))
        self.assertEqual(sorted(trail), sorted([Event.KEY_REGISTERED, Event.KEY_ROTATED, Event.KEY_REVOKED]))

    def test_audit_log_is_append_only(self):
        self._sealer("sealer-1")
        log = SealerKeyAuditLog.objects.get()
        log.rotation_reason = "diubah"
        with self.assertRaises(LedgerWriteForbidden):
            log.save()
        with self.assertRaises(LedgerWriteForbidden):
            log.delete()
        with self.assertRaises(LedgerWriteForbidden):
            SealerKeyAuditLog.objects.all().delete()
        with self.assertRaises(LedgerWriteForbidden):
            SealerKeyAuditLog.objects.update(rotation_reason="x")

    def test_sealing_is_audited_and_rotation_keeps_history_valid(self):
        sealer, private = self._sealer("sealer-1")
        block = self._seal([{"sealer_id": sealer.id, "private_key": private}])
        used = SealerKeyAuditLog.objects.filter(event_type=Event.KEY_USED, sealer_id=sealer.id)
        self.assertTrue(used.filter(block_at_rotation=block.block_no).exists())

        svc.rotate_sealer_key(sealer.id, svc.generate_sealer_keypair()[1])
        rotated = SealerKeyAuditLog.objects.get(event_type=Event.KEY_ROTATED)
        self.assertEqual(rotated.block_at_rotation, block.block_no)
        self.assertEqual(svc.verify_blocks(), (True, None))       # blok lama tetap sah

        svc.revoke_sealer_key(SealerKey.objects.get(previous_key=sealer).id)
        self.assertEqual(svc.verify_blocks(), (True, None))       # revoke key BARU: blok lama aman
        SealerKey.objects.filter(pk=sealer.pk).update(status=SealerKey.KeyStatus.REVOKED)
        self.assertEqual(svc.verify_blocks()[0], False)           # key penanda blok bocor

    def test_forged_and_duplicate_signatures_are_not_counted(self):
        sealer, private = self._sealer("sealer-1")
        block = self._seal([{"sealer_id": sealer.id, "private_key": private}])
        sig = block.signatures[0]
        self.assertEqual(svc.verify_block_signatures(block, threshold=1), (True, 1))
        block.signatures = [sig, dict(sig)]                       # duplikat
        self.assertEqual(svc.verify_block_signatures(block, threshold=2), (False, 1))
        rogue_private, rogue_public = svc.generate_sealer_keypair()
        rogue_sig = nacl_signing.SigningKey(bytes.fromhex(rogue_private)).sign(
            bytes(block.block_hash)).signature.hex()
        block.signatures = [sig, {"sealer_id": str(sealer.id), "public_key": rogue_public,
                                  "signature": rogue_sig}]          # key tak terdaftar
        self.assertEqual(svc.verify_block_signatures(block, threshold=2), (False, 1))


class MultiRegionSealerTests(SealerKeyTestCase):
    def test_cosign_from_second_region_satisfies_min_regions(self):
        jkt, jkt_key = self._sealer("sealer-jkt", region="ap-southeast-3")
        sgp, sgp_key = self._sealer("sealer-sgp", region="ap-southeast-1")
        block = self._seal([{"sealer_id": jkt.id, "private_key": jkt_key}])
        self.assertEqual(block.signatures[0]["region"], "ap-southeast-3")
        self.assertEqual(svc.verify_blocks(threshold=2, min_regions=2), (False, 1))

        self.assertEqual([b.block_no for b in svc.pending_cosign_blocks(jkt.id)], [])
        self.assertIn(block.block_no, [b.block_no for b in svc.pending_cosign_blocks(sgp.id)])
        for b in svc.pending_cosign_blocks(sgp.id):
            svc.cosign_block(b.block_no, sgp.id, sgp_key)
        self.assertEqual(list(svc.pending_cosign_blocks(sgp.id)), [])
        self.assertEqual(svc.verify_blocks(threshold=2, min_regions=2), (True, None))
        self.assertEqual({s.region for s in svc.valid_block_signers(block)},
                         {"ap-southeast-1", "ap-southeast-3"})
        self.assertTrue(SealerKeyAuditLog.objects.filter(
            event_type=Event.KEY_USED, sealer_id=sgp.id, metadata__mode="cosign").exists())

        with self.assertRaises(svc.InvalidState):
            svc.cosign_block(block.block_no, sgp.id, sgp_key)    # dua kali
        with self.assertRaises(svc.InvalidState):
            svc.cosign_block(block.block_no, jkt.id, jkt_key)    # sudah menyegel
        with self.assertRaises(svc.InvalidInput):
            svc.cosign_block(block.block_no, sgp.id, jkt_key)    # key salah
        with self.assertRaises(LedgerWriteForbidden):
            BlockSignature.objects.all().delete()

    def test_cosign_ledger_command(self):
        jkt, jkt_key = self._sealer("sealer-jkt", region="ap-southeast-3")
        sgp, sgp_key = self._sealer("sealer-sgp", region="ap-southeast-1")
        self._seal([{"sealer_id": jkt.id, "private_key": jkt_key}])
        out = StringIO()
        with _env(SGP_KEY=sgp_key):
            call_command("cosign_ledger", "--sealer-id", str(sgp.id), "--key-env", "SGP_KEY", "--once",
                         stdout=out)
        self.assertIn("region=ap-southeast-1", out.getvalue())
        self.assertEqual(svc.verify_blocks(threshold=2, min_regions=2), (True, None))
        self.assertEqual(svc.get_active_sealers(region="ap-southeast-1").get(), sgp)


class _env:
    def __init__(self, **values):
        self.values, self.old = values, {}

    def __enter__(self):
        for k, v in self.values.items():
            self.old[k] = os.environ.get(k)
            os.environ[k] = v

    def __exit__(self, *exc):
        for k, v in self.old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


class ShamirTests(SimpleTestCase):
    def test_any_threshold_subset_recovers_secret(self):
        secret = bytes(range(32))
        shares = shamir.split_secret(secret, 5, 3)
        for subset in itertools.permutations(shares, 3):
            self.assertEqual(shamir.combine_shares(list(subset)), secret)
        self.assertEqual(shamir.combine_shares(shares), secret)

    def test_too_few_mixed_or_corrupt_shares_are_rejected(self):
        a = shamir.split_secret(b"\x01" * 32, 3, 2)
        b = shamir.split_secret(b"\x01" * 32, 3, 2)
        with self.assertRaises(shamir.ShamirError):
            shamir.combine_shares(a[:1])
        with self.assertRaises(shamir.ShamirError):
            shamir.combine_shares([a[0], b[1]])
        corrupt = a[0][:-12] + ("0" if a[0][-12] != "0" else "1") + a[0][-11:]
        with self.assertRaises(shamir.ShamirError):
            shamir.combine_shares([corrupt, a[1]])
        with self.assertRaises(shamir.ShamirError):
            shamir.split_secret(b"x", 2, 3)


class SealerShamirServiceTests(SealerKeyTestCase):
    def test_split_and_recover_are_audited_without_leaking_secret(self):
        sealer, private = self._sealer("sealer-1")
        shares = svc.split_sealer_private_key(sealer.id, private, shares=5, threshold=3)
        self.assertEqual(svc.recover_sealer_private_key(sealer.id, shares[1:4]), private)

        split_log = SealerKeyAuditLog.objects.get(event_type=Event.KEY_SPLIT)
        self.assertEqual((split_log.metadata["shares"], split_log.metadata["threshold"]), (5, 3))
        self.assertEqual(len(split_log.metadata["share_fingerprints"]), 5)
        self.assertTrue(SealerKeyAuditLog.objects.filter(event_type=Event.KEY_RECOVERED).exists())
        dump = str(list(SealerKeyAuditLog.objects.values()))
        self.assertNotIn(private, dump)
        for share in shares:
            self.assertNotIn(share, dump)

        other, other_private = self._sealer("sealer-2")
        with self.assertRaises(svc.InvalidInput):
            svc.recover_sealer_private_key(other.id, shares[:3])  # share milik sealer lain
        with self.assertRaises(svc.InvalidInput):
            svc.recover_sealer_private_key(sealer.id, shares[:2])  # kurang dari threshold
        with self.assertRaises(svc.InvalidInput):
            svc.split_sealer_private_key(sealer.id, other_private, 3, 2)

    def test_recovered_key_can_seal_and_command_roundtrip(self):
        sealer, private = self._sealer("sealer-1")
        with TemporaryDirectory() as tmp, _env(SEALER_KEY=private):
            out = StringIO()
            call_command("sealer_shamir", "split", "--sealer-id", str(sealer.id), "--key-env", "SEALER_KEY",
                         "--shares", "3", "--threshold", "2", "--out-dir", tmp, stdout=out)
            files = sorted(Path(tmp).iterdir())
            self.assertEqual(len(files), 3)
            self.assertEqual(files[0].stat().st_mode & 0o777, 0o600)
            out = StringIO()
            call_command("sealer_shamir", "check", "--sealer-id", str(sealer.id),
                         "--share-file", str(files[0]), "--share-file", str(files[2]), stdout=out)
            self.assertIn("OK", out.getvalue())
            shares = [files[1].read_text().strip(), files[2].read_text().strip()]
        recovered = svc.recover_sealer_private_key(sealer.id, shares)
        block = self._seal([{"sealer_id": sealer.id, "private_key": recovered}])
        self.assertEqual(svc.verify_block_signatures(block), (True, 1))


class SignatureEnforcementTests(SealerKeyTestCase):
    def test_unsigned_or_unregistered_sealing_is_refused(self):
        svc.create_entry(self.assoc, self.member, "uji", 1)
        with self.assertRaises(svc.InvalidInput):
            svc.seal_next_block(window=timedelta(seconds=1), safety_lag=timedelta(0))
        rogue_private, _ = svc.generate_sealer_keypair()
        with self.assertRaises(svc.InvalidInput):
            svc.seal_next_block(window=timedelta(seconds=1), safety_lag=timedelta(0),
                                sealer_private_keys=rogue_private)
        self.assertFalse(svc.Block.objects.exists())

    def test_legacy_key_without_sealer_id_is_resolved_by_public_key(self):
        sealer, private = self._sealer("sealer-1")
        block = self._seal(private)
        self.assertEqual(block.signatures[0]["sealer_id"], str(sealer.id))
        self.assertEqual(svc.verify_blocks(), (True, None))

    def test_unsigned_block_fails_verification(self):
        with override_settings(LEDGER_REQUIRE_BLOCK_SIGNATURES=False):
            self._seal()
            self.assertEqual(svc.verify_blocks(), (True, None))
        self.assertEqual(svc.verify_blocks(), (False, 1))


def _anchor_settings(path):
    return override_settings(LEDGER_ANCHOR_INTERVAL=1, LEDGER_ANCHOR_BACKENDS=[
        {"NAME": "file", "BACKEND": "snailstamp.apps.ledger.anchors.FileAnchorBackend",
         "OPTIONS": {"path": str(path)}}])


class AnchorAndForkDetectionTests(SealerKeyTestCase):
    def setUp(self):
        super().setUp()
        self.tmp = TemporaryDirectory()
        self.path = Path(self.tmp.name) / "anchors.jsonl"
        self.settings_cm = _anchor_settings(self.path)
        self.settings_cm.enable()
        self.sealer, self.private = self._sealer("sealer-1", region="ap-southeast-3")
        self.keys = [{"sealer_id": self.sealer.id, "private_key": self.private}]

    def tearDown(self):
        self.settings_cm.disable()
        self.tmp.cleanup()

    def _tamper(self, sql, params):
        with connection.cursor() as cur:
            cur.execute("SET LOCAL session_replication_role = replica")
            cur.execute(sql, params)
            cur.execute("SET LOCAL session_replication_role = DEFAULT")

    def test_anchor_publishes_signed_checkpoint_and_respects_interval(self):
        block = self._seal(self.keys)
        (anchor,) = svc.anchor_blocks()
        self.assertEqual((anchor.block_no, anchor.backend), (block.block_no, "file"))
        (line,) = self.path.read_text().splitlines()
        cp = json.loads(line)
        self.assertEqual(cp["block_hash"], bytes(block.block_hash).hex())
        self.assertEqual(cp["signatures"][0]["public_key"], self.sealer.public_key)
        self.assertEqual(svc.anchor_blocks(), [])                  # belum ada blok baru
        self.assertEqual(svc.detect_forks(), [])
        with override_settings(LEDGER_ANCHOR_INTERVAL=1000):
            self._seal(self.keys)
            self.assertEqual(svc.anchor_blocks(), [])              # belum sampai interval
            self.assertEqual(len(svc.anchor_blocks(force=True)), 1)
        with self.assertRaises(LedgerWriteForbidden):
            BlockAnchor.objects.all().delete()

    def test_broken_chain_is_never_anchored(self):
        self._seal(self.keys)
        SealerKey.objects.filter(pk=self.sealer.pk).update(status=SealerKey.KeyStatus.REVOKED)
        with self.assertRaises(svc.InvalidState):
            svc.anchor_blocks()
        self.assertFalse(self.path.exists())

    @skipUnless(_is_superuser(), "perlu superuser untuk melewati trigger")
    def test_rewrite_and_rollback_are_detected(self):
        block = self._seal(self.keys)
        svc.anchor_blocks()
        forged = hashlib.sha256(b"riwayat palsu").digest()
        self._tamper("UPDATE ledger_blocks SET block_hash = %s WHERE block_no = %s", [forged, block.block_no])
        kinds = {f.kind for f in svc.detect_forks()}
        self.assertEqual(kinds, {"hash_mismatch", "chain_invalid"})
        self._tamper("DELETE FROM ledger_blocks WHERE block_no = %s", [block.block_no])
        self.assertIn("missing_block", {f.kind for f in svc.detect_forks()})

    @skipUnless(_is_superuser(), "perlu superuser untuk melewati trigger")
    def test_equivocation_by_sealer_key_is_detected(self):
        """Pencuri key menulis ulang blok DAN menandatanganinya ulang: chain di DB konsisten lagi,
        tapi checkpoint lama membuktikan key yang sama menandatangani dua riwayat."""
        block = self._seal(self.keys)
        svc.anchor_blocks()
        b = svc.Block.objects.get(pk=block.block_no)
        new_end = b.window_end + timedelta(microseconds=1)
        new_hash = svc._block_hash(bytes(b.prev_block_hash), bytes(b.merkle_root), b.window_start, new_end,
                                   b.log_count)
        sig = nacl_signing.SigningKey(bytes.fromhex(self.private)).sign(new_hash).signature.hex()
        sigs = [{"sealer_id": str(self.sealer.id), "public_key": self.sealer.public_key, "signature": sig}]
        self._tamper("UPDATE ledger_blocks SET window_end = %s, block_hash = %s, signatures = %s::jsonb "
                     "WHERE block_no = %s", [new_end, new_hash, json.dumps(sigs), b.block_no])
        findings = svc.detect_forks()
        self.assertEqual([f.kind for f in findings], ["equivocation"])
        self.assertIn(self.sealer.public_key, findings[0].detail)

    def test_forged_checkpoint_and_corrupt_backend_are_reported(self):
        block = self._seal(self.keys)
        forged = anchors.Checkpoint(block_no=block.block_no, block_hash="00" * 32,
                                    prev_block_hash="00" * 32, window_end_us=0,
                                    signatures=[{"public_key": self.sealer.public_key, "signature": "00" * 64}])
        anchors.FileAnchorBackend("file", path=str(self.path)).publish(forged)
        self.assertEqual([f.kind for f in svc.detect_forks()], ["invalid_checkpoint"])
        with self.path.open("a") as fh:
            fh.write("bukan json\n")
        self.assertEqual([f.kind for f in svc.detect_forks()], ["backend_error"])

    def test_conflicting_backends_are_reported(self):
        self._seal(self.keys)
        other = Path(self.tmp.name) / "other.jsonl"
        backends = [anchors.FileAnchorBackend("a", path=str(self.path)),
                    anchors.FileAnchorBackend("b", path=str(other))]
        svc.anchor_blocks(backends=backends)
        cp = json.loads(other.read_text())
        cp["block_hash"] = "11" * 32                               # tanpa signature valid -> ditolak
        other.write_text(json.dumps(cp) + "\n")
        self.assertEqual([f.kind for f in svc.detect_forks(backends=backends)], ["invalid_checkpoint"])
        cp["signatures"][0]["signature"] = nacl_signing.SigningKey(bytes.fromhex(self.private)).sign(
            bytes.fromhex(cp["block_hash"])).signature.hex()       # key yang sama menandatangani versi lain
        other.write_text(json.dumps(cp) + "\n")
        kinds = sorted(f.kind for f in svc.detect_forks(backends=backends))
        self.assertEqual(kinds, ["anchor_conflict", "equivocation"])
        self.assertEqual(svc.detect_forks(backends=backends[:1]), [])

    def test_http_backend_roundtrip(self):
        store, seen_auth = [], []

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                seen_auth.append(self.headers.get("Authorization"))
                store.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
                self._reply(f"receipt-{len(store)}")

            def do_GET(self):
                self._reply(json.dumps(store))

            def _reply(self, text):
                body = text.encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            backend = anchors.HttpAnchorBackend("notary", url=f"http://127.0.0.1:{server.server_port}/",
                                                token_env="ANCHOR_TOKEN")
            self._seal(self.keys)
            with _env(ANCHOR_TOKEN="rahasia"):
                (anchor,) = svc.anchor_blocks(backends=[backend])
            self.assertEqual((anchor.receipt, seen_auth), ("receipt-1", ["Bearer rahasia"]))
            self.assertEqual(svc.detect_forks(backends=[backend]), [])
        finally:
            server.shutdown()
            server.server_close()
        self.assertEqual([f.kind for f in svc.detect_forks(backends=[backend])], ["backend_error"])

    def test_commands(self):
        self._seal(self.keys)
        out = StringIO()
        call_command("anchor_ledger", "--once", stdout=out)
        self.assertEqual(json.loads(out.getvalue())["backend"], "file")
        out = StringIO()
        call_command("check_forks", stdout=out)
        self.assertIn("OK", out.getvalue())
        cp = json.loads(self.path.read_text())
        cp["block_no"] += 100                                      # checkpoint untuk blok yang "hilang"
        with self.path.open("a") as fh:
            fh.write(json.dumps(cp) + "\n")
        with self.assertRaises(CommandError):
            call_command("check_forks", stdout=StringIO(), stderr=StringIO())


class LightClientSignatureTests(SealerKeyTestCase):
    def _proof(self):
        """Segel blok bertanda tangan JKT, return bukti log MINT (sudah lewat JSON, seperti di client)."""
        self.jkt, self.jkt_key = self._sealer("sealer-jkt", region="ap-southeast-3")
        self.sgp, self.sgp_key = self._sealer("sealer-sgp", region="ap-southeast-1")
        _, (coll,) = svc.create_item(self.assoc, self.member, "pena", 1, "pen")
        time.sleep(2.2)
        keys = [{"sealer_id": self.jkt.id, "private_key": self.jkt_key}]
        while svc.seal_next_block(window=timedelta(seconds=1), safety_lag=timedelta(0), sealer_private_keys=keys):
            pass
        self.coll = coll
        return json.loads(json.dumps(svc.build_proof(coll, 1)))

    def test_proof_carries_signatures_verified_against_pinned_keys(self):
        proof = self._proof()
        (sig,) = proof["block"]["signatures"]
        self.assertEqual((sig["public_key"], sig["region"]), (self.jkt.public_key, "ap-southeast-3"))
        trusted = svc.trusted_sealer_keys()
        self.assertTrue(svc.verify_proof(proof, trusted_keys=trusted))
        self.assertTrue(svc.verify_proof(proof, trusted_keys=[self.jkt.public_key]))
        self.assertTrue(svc.verify_proof(proof))                   # tanpa pin: cukup ada signature valid
        self.assertFalse(svc.verify_proof(proof, trusted_keys={self.sgp.public_key: "ap-southeast-1"}))
        self.assertFalse(svc.verify_proof(proof, trusted_keys=trusted, threshold=2))

        svc.revoke_sealer_key(self.jkt.id)                         # key bocor -> tidak dipercaya lagi
        self.assertFalse(svc.verify_proof(proof, trusted_keys=svc.trusted_sealer_keys()))

    def test_stripped_forged_or_rogue_signatures_fail(self):
        proof = self._proof()
        trusted = svc.trusted_sealer_keys()
        stripped = json.loads(json.dumps(proof))
        stripped["block"]["signatures"] = []
        self.assertFalse(svc.verify_proof(stripped))
        del stripped["block"]["signatures"]
        self.assertFalse(svc.verify_proof(stripped))
        forged = json.loads(json.dumps(proof))
        forged["block"]["signatures"][0]["signature"] = "00" * 64
        self.assertFalse(svc.verify_proof(forged, trusted_keys=trusted))
        rogue_private, rogue_public = svc.generate_sealer_keypair()
        rogue = json.loads(json.dumps(proof))
        rogue["block"]["signatures"] = [{"public_key": rogue_public, "region": "ap-southeast-3",
                                         "signature": nacl_signing.SigningKey(bytes.fromhex(rogue_private)).sign(
                                             bytes.fromhex(proof["block"]["block_hash"])).signature.hex()}]
        self.assertTrue(svc.verify_proof(rogue))                   # ditandatangani, tapi oleh siapa?
        self.assertFalse(svc.verify_proof(rogue, trusted_keys=trusted))
        wrong_block = json.loads(json.dumps(proof))
        wrong_block["block"]["block_hash"] = "11" * 32             # signature terikat ke hash blok
        self.assertFalse(svc.verify_proof(wrong_block))

    def test_multi_region_threshold_uses_trusted_regions(self):
        proof = self._proof()
        trusted = svc.trusted_sealer_keys()
        self.assertFalse(svc.verify_proof(proof, trusted_keys=trusted, threshold=2, min_regions=2))
        for b in svc.pending_cosign_blocks(self.sgp.id):
            svc.cosign_block(b.block_no, self.sgp.id, self.sgp_key)
        proof = json.loads(json.dumps(svc.build_proof(self.coll, 1)))
        self.assertEqual({s["region"] for s in proof["block"]["signatures"]}, {"ap-southeast-1", "ap-southeast-3"})
        self.assertTrue(svc.verify_proof(proof, trusted_keys=trusted, threshold=2, min_regions=2))
        for sig in proof["block"]["signatures"]:
            sig["region"] = "palsu"                                # region di bukti tidak dipercaya
        self.assertTrue(svc.verify_proof(proof, trusted_keys=trusted, threshold=2, min_regions=2))
        same_region = dict.fromkeys(trusted, "ap-southeast-1")
        self.assertFalse(svc.verify_proof(proof, trusted_keys=same_region, threshold=2, min_regions=2))

    def test_export_sealer_keys_command(self):
        self._sealer("sealer-1", region="ap-southeast-3")
        out = StringIO()
        call_command("export_sealer_keys", stdout=out)
        self.assertEqual(json.loads(out.getvalue()), svc.trusted_sealer_keys())
