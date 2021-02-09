from django.conf import settings
from django.contrib import auth as dj_auth
from django.contrib.auth import password_validation
from django.contrib.auth.models import update_last_login
from django.utils.translation import gettext as _
from hashid_field import rest
from rest_framework import exceptions, serializers, validators
from rest_framework_simplejwt.exceptions import InvalidToken
from rest_framework_simplejwt.serializers import TokenRefreshSerializer
from rest_framework_simplejwt.settings import api_settings as jwt_api_settings
from rest_framework_simplejwt.tokens import RefreshToken

from . import models, tokens, notifications


class UserProfileSerializer(serializers.ModelSerializer):
    id = rest.HashidSerializerCharField(source_field="users.User.id", source="user.id", read_only=True)
    email = serializers.CharField(source="user.email", read_only=True)
    roles = serializers.SerializerMethodField()

    def get_roles(self, obj):
        return [group.name for group in obj.user.groups.all()]

    class Meta:
        model = models.UserProfile
        fields = ("id", "first_name", "last_name", "email", "roles")


class UserSignupSerializer(serializers.ModelSerializer):
    id = rest.HashidSerializerCharField(source_field="users.User.id", read_only=True)
    email = serializers.EmailField(
        validators=[validators.UniqueValidator(queryset=dj_auth.get_user_model().objects.all())],
    )
    access = serializers.CharField(read_only=True)
    refresh = serializers.CharField(read_only=True)

    class Meta:
        model = dj_auth.get_user_model()
        fields = ("id", "email", "password", "access", "refresh")
        extra_kwargs = {"password": {"write_only": True}}

    def validate_password(self, password):
        password_validation.validate_password(password)
        return password

    def create(self, validated_data):
        user = dj_auth.get_user_model().objects.create_user(
            validated_data["email"],
            validated_data["password"],
        )

        refresh = RefreshToken.for_user(user)

        if jwt_api_settings.UPDATE_LAST_LOGIN:
            update_last_login(None, user)

        notifications.create(
            "account_activation",
            user,
            data=notifications.AccountActivationNotificationData(
                user_id=user.id.hashid, token=tokens.account_activation_token.make_token(user)
            ),
        )

        return {'id': user.id, 'email': user.email, 'access': str(refresh.access_token), 'refresh': str(refresh)}


class UserAccountConfirmationSerializer(serializers.Serializer):
    user = serializers.PrimaryKeyRelatedField(
        queryset=models.User.objects.all(),
        pk_field=rest.HashidSerializerCharField(),
        write_only=True,
    )
    token = serializers.CharField(write_only=True)

    def validate(self, attrs):
        token = attrs["token"]
        user = attrs["user"]

        if not tokens.account_activation_token.check_token(user, token):
            raise exceptions.ValidationError(_("Malformed user account confirmation token"))

        return attrs

    def update(self, instance, validated_data):
        pass

    def create(self, validated_data):
        user = validated_data.pop("user")
        user.is_confirmed = True
        user.save()
        return user


class UserAccountChangePasswordSerializer(serializers.Serializer):
    user = serializers.HiddenField(default=serializers.CurrentUserDefault())
    old_password = serializers.CharField(write_only=True, help_text=_("Old password"))
    new_password = serializers.CharField(write_only=True, help_text=_("New password"))

    refresh = serializers.CharField(read_only=True)
    access = serializers.CharField(read_only=True)

    def validate_new_password(self, new_password):
        password_validation.validate_password(new_password)
        return new_password

    def validate(self, attrs):
        old_password = attrs["old_password"]

        user = attrs["user"]
        if not user.check_password(old_password):
            raise exceptions.ValidationError({"old_password": _("Wrong old password")})

        return attrs

    def update(self, instance, validated_data):
        pass

    def create(self, validated_data):
        user = validated_data.pop("user")
        new_password = validated_data.pop("new_password")
        user.set_password(new_password)
        user.save()

        refresh = RefreshToken.for_user(user)

        return {
            'access': str(refresh.access_token),
            'refresh': str(refresh),
        }


class PasswordResetSerializer(serializers.Serializer):
    email = serializers.EmailField(write_only=True, help_text=_("User e-mail"))

    def create(self, validated_data):
        try:
            user = dj_auth.get_user_model().objects.get(email=validated_data["email"])
        except dj_auth.get_user_model().DoesNotExist:
            raise exceptions.NotFound(_("User not found"))

        notifications.create(
            "password_reset",
            user=user,
            data=notifications.PasswordResetNotificationData(
                user_id=user.id.hashid, token=tokens.password_reset_token.make_token(user)
            ),
        )

        return user

    def update(self, instance, validated_data):
        pass


class PasswordResetConfirmationSerializer(serializers.Serializer):
    user = serializers.PrimaryKeyRelatedField(
        queryset=models.User.objects.all(),
        pk_field=rest.HashidSerializerCharField(),
        write_only=True,
    )

    new_password = serializers.CharField(write_only=True, help_text=_("New password"))
    token = serializers.CharField(write_only=True, help_text=_("Token"))

    def validate_new_password(self, new_password):
        password_validation.validate_password(new_password)
        return new_password

    def validate(self, attrs):
        token = attrs["token"]
        user = attrs["user"]

        if not tokens.password_reset_token.check_token(user, token):
            raise exceptions.ValidationError(_("Malformed password reset token"))

        return attrs

    def update(self, instance, validated_data):
        pass

    def create(self, validated_data):
        user = validated_data.pop("user")
        new_password = validated_data.pop("new_password")
        user.set_password(new_password)
        user.save()

        return user


class CookieTokenRefreshSerializer(TokenRefreshSerializer):
    refresh = None

    def validate(self, attrs):
        refresh = self.context['request'].COOKIES.get(settings.REFRESH_TOKEN_COOKIE)
        if refresh:
            return super().validate(
                {
                    'refresh': refresh,
                    **attrs,
                }
            )
        else:
            raise InvalidToken('No valid token found in cookie \'refresh_token\'')
