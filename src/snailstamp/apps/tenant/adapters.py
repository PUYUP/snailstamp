from allauth.account.adapter import DefaultAccountAdapter
from allauth.socialaccount.adapter import DefaultSocialAccountAdapter


class AccountAdapter(DefaultAccountAdapter):
    pass


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
