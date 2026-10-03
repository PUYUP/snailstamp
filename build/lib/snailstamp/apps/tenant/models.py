from django.contrib.auth.models import AbstractUser
from organizations.abstract import (
    AbstractOrganization,
    AbstractOrganizationUser,
    AbstractOrganizationOwner,
    AbstractOrganizationInvitation,
)
from snailstamp.core.models import TimeMixin


class User(AbstractUser):
    pass


class Issuer(TimeMixin, AbstractOrganization):
    pass


class Member(TimeMixin, AbstractOrganizationUser):
    pass


class Owner(TimeMixin, AbstractOrganizationOwner):
    pass


class Invitation(TimeMixin, AbstractOrganizationInvitation):
    pass
