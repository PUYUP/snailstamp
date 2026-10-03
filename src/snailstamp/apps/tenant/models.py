import uuid

from django.contrib.auth.models import AbstractUser
from django.db import models
from organizations.abstract import (
    AbstractOrganization,
    AbstractOrganizationUser,
    AbstractOrganizationOwner,
    AbstractOrganizationInvitation,
)
from snailstamp.core.models import TimeMixin


class User(AbstractUser):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)


class Association(TimeMixin, AbstractOrganization):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)


class Member(TimeMixin, AbstractOrganizationUser):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)


class Owner(TimeMixin, AbstractOrganizationOwner):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)


class Invitation(TimeMixin, AbstractOrganizationInvitation):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
