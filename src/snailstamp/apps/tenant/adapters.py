from allauth.account.adapter import DefaultAccountAdapter
from allauth.socialaccount.adapter import DefaultSocialAccountAdapter
from allauth.socialaccount.models import SocialAccount
from django.conf import settings


class AccountAdapter(DefaultAccountAdapter):
    def save_user(self, request, user, form, commit=True):
        data = form.cleaned_data
        user.email = data.get('email')
        user.first_name = data.get('first_name', '')
        user.last_name = data.get('last_name', '')
        user.username = user.email
        if 'password' in data:
            user.set_password(data['password'])
        else:
            user.set_unusable_password()
        if commit:
            user.save()
        return user


class SocialAccountAdapter(DefaultSocialAccountAdapter):
    def pre_social_login(self, request, sociallogin):
        if sociallogin.is_existing:
            return

        if 'email' not in sociallogin.account.extra_data:
            return

        email = sociallogin.account.extra_data['email']
        try:
            user = sociallogin.user
            user.email = email
            user.username = email
            user.save()
        except Exception:
            pass

    def get_connect_redirect_url(self, request, socialaccount):
        return request.build_absolute_uri('/api/v1/auth/success/')
