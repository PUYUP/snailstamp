from django.test import TestCase
from django.contrib.contenttypes.models import ContentType
from snailstamp.apps.tenant.models import Association, User, Certificate
from snailstamp.apps.vault.models import Collection, CollectionType
from snailstamp.apps.tenant.signer import verify_collection


class VaultTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        # Dibuat sekali per class, lebih cepat dari setUp
        cls.association = Association.objects.create(
            name="Test Association",
            slug="test-association",
        )

        cls.collection_type = CollectionType.objects.create(
            name="Pen",
            slug="pen"
        )
        cls.user = User.objects.create_user(
            username="test-user",
            email="test@user.com",
            password="password"
        )
        cls.certificate = None
        cls.collection_a = None

    def test_create_association(self):
        self.assertEqual(self.association.slug, 'test-association')

    def test_association_certificate_is_created(self):
        ct = ContentType.objects.get_for_model(self.association)
        
        self.certificate = Certificate.objects.filter(association_content_type=ct, association_object_id=self.association.id).first()
        self.assertIsNotNone(self.certificate)

    def test_collection(self):
        self.collection_a = Collection.objects.create(
            name="Test Collection",
            slug="collection-a",
            association=self.association,
            collection_type=self.collection_type,
            assigner=self.user,
            properties={},
        )

        self.collection_b = Collection.objects.create(
            name="Test Collection B",
            slug="collection-b",
            association=self.association,
            collection_type=self.collection_type,
            assigner=self.user,
            properties={},
        )

        self.assertIsNotNone(self.collection_a)
        self.assertIsNotNone(self.collection_b)

        # check signature
        signature = self.collection_a.signatures.first()
        self.assertIsNotNone(signature)

        # check signature of collection_b
        signature_b = self.collection_b.signatures.first()
        self.assertIsNotNone(signature_b)

        # validate collection
        is_valid = verify_collection(self.collection_a, signature)
        self.assertTrue(is_valid)

        # collection lain tidak boleh pakai signature yang sama
        is_valid = verify_collection(self.collection_b, signature)
        self.assertFalse(is_valid)

        # bisa untuk collection sendiri
        is_valid = verify_collection(self.collection_b, signature_b)
        self.assertTrue(is_valid)

        # modify collection_b
        self.collection_b.name = "Test Collection B Modified"
        self.collection_b.save()
        
        is_valid = verify_collection(self.collection_b, signature)
        self.assertFalse(is_valid)