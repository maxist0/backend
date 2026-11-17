"""Error codes of the auth module."""

from app.core import AppError, BadRequest, Conflict, Unauthorized


class UserExistsError(Conflict):
    """The username or the email address is already registered."""

    code = "user_exists"
    detail = "The username or email address is already registered."


class InvalidCredentialsError(Unauthorized):
    """Login refused.

    One answer for an unknown user, a wrong password, a locked and an inactive
    account, so the response does not reveal whether an account exists.
    """

    code = "invalid_credentials"
    detail = "Invalid username or password"


class InvalidRefreshTokenError(Unauthorized):
    """The refresh cookie is missing, unknown, expired, revoked or reused."""

    code = "invalid_refresh_token"
    detail = "Invalid or expired refresh token"


class SessionNotFoundError(BadRequest):
    """Logout with a refresh cookie that matches no session."""

    code = "session_not_found"
    detail = "Session not found"


class InvalidTokenError(BadRequest):
    """An email token (verification, email change, password reset) is
    unknown, used, invalidated or expired."""

    code = "invalid_token"
    detail = "Invalid or expired token"


class WrongPasswordError(BadRequest):
    """The current password given to confirm a change is wrong."""

    code = "wrong_password"
    detail = "Current password is incorrect"


class EmailTakenError(Conflict):
    """The new email address belongs to another account."""

    code = "email_taken"
    detail = "Email already in use"


class EmailUnchangedError(BadRequest):
    """The new email address is the current one."""

    code = "email_unchanged"
    detail = "The new email address is the current address"


class EmailAlreadyVerifiedError(BadRequest):
    """A verification email was requested for a verified address."""

    code = "email_already_verified"
    detail = "Email is already verified"


class TwoFactorNotImplementedError(AppError):
    """Two-factor authentication is not available yet."""

    status = 501
    code = "not_implemented"
    detail = "Two-factor authentication is not yet implemented"
