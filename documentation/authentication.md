# Authentication System

> **Status (2026-11-16):** this page is partly stale. Paths, response bodies and token formats below predate the review. The current behaviour is described in [README.md → Authentication design](../README.md#authentication-design) and [Security posture](../README.md#security-posture). Corrected in this page: refresh tokens (Postgres only), replay detection, session revocation, `logout-all`, password hashing settings.

## Overview

This application implements a comprehensive, production-ready authentication system with:

- **Password-based authentication** with Argon2 hashing
- **JWT tokens** (access + refresh) for stateless, scalable sessions
- **Token rotation** with family-based replay detection
- **Email verification** for account registration
- **Password reset** via email tokens
- **Brute-force protection** with account locking
- **Multi-session support** with device tracking
- **Comprehensive audit logging** for security monitoring

## Authentication Flow Diagram

```
User Registration
  ├─ POST /api/auth/register (username, email, password)
  ├─ Hash password with Argon2
  ├─ Create user account (is_email_verified=false)
  ├─ Generate email verification token
  ├─ Send email with verification link
  └─ Return: User created, check email

User Email Verification
  ├─ User clicks link with token
  ├─ GET /api/auth/verify-email?token=...
  ├─ Verify token hash matches, not expired
  ├─ Update is_email_verified=true
  └─ Return: Email verified, can now login

User Login (Password)
  ├─ POST /api/auth/login (username/email, password)
  ├─ Find user by username or email
  ├─ Check if account is locked (timed lock or admin lock)
  │  └─ If locked and locked_until < now: unlock
  │  └─ If still locked: reject with "account locked" error
  ├─ Wait for the admission limiter (PASSWORD_HASH_CONCURRENCY per process), then re-check the lock
  ├─ Verify password hash matches
  ├─ If password invalid: increment failed_login_attempts (atomic)
  │  └─ If >= MAXIMUM_LOGIN_ATTEMPTS (5): set is_locked=true,
  │     locked_until=now+LOGIN_ATTEMPT_WINDOW_SECONDS (timed lock)
  ├─ If password valid: conditional atomic UPDATE resets failed_login_attempts=0
  │  (refused if a lock was committed meanwhile)
  ├─ Create session with token family
  ├─ Generate JWT access + refresh tokens
  ├─ Store session in database
  ├─ Log auth event: LOGIN_SUCCESS
  └─ Return: access_token, refresh_token, expires_in

Auto-Refresh (Access Token Expires)
  ├─ User makes request with expired access_token
  ├─ GET /api/user (Authorization: Bearer expired_token)
  ├─ Check raises InvalidTokenError
  ├─ Client detects: POST /api/auth/refresh (refresh_token)
  ├─ Atomic UPDATE ... RETURNING on the session row (sessions table, Postgres only)
  │  └─ matches only if not revoked and not expired
  ├─ If the token was already rotated or revoked: reuse detected, revoke the whole family
  ├─ If the user is inactive or admin-locked: refresh denied, family revoked (a timed lock does not affect refresh)
  ├─ Issue new access_token + refresh_token in the same family
  └─ Return: new access_token, new refresh_token

Logout
  ├─ POST /api/auth/logout (via Authorization header)
  ├─ Get user from JWT token
  ├─ Find session by token_hash
  ├─ Set is_revoked=true, revoke_reason="user_logout"
  └─ Return: Logout successful

Password Reset
  ├─ POST /api/auth/forgot-password (email)
  ├─ Find user by email
  ├─ Generate password reset token
  ├─ Create email_token with purpose='RESET_PASSWORD'
  ├─ Send email with reset link
  ├─ Log event: PASSWORD_RESET_REQUESTED
  └─ Return: Check your email

  ├─ User clicks link: /auth/reset-password?token=...
  ├─ Verify token hash, not expired
  ├─ POST /api/auth/reset-password (token, new_password)
  ├─ Hash new password
  ├─ Update password_credentials
  ├─ Revoke all existing sessions (security)
  ├─ Mark token as used
  ├─ Log event: PASSWORD_RESET_COMPLETED
  └─ Return: Password reset successful

OAuth / Social Login (Future)
  ├─ POST /api/auth/oauth/google
  ├─ Verify Google ID token
  ├─ Find or create user
  └─ Return: access_token, refresh_token
```

## Core Models

### User Model

See [database_schemas/user.md](database_schemas/user.md) for complete schema.

Key fields:
- `is_active`: Whether account is operational
- `is_email_verified`: Email verification status
- `is_locked`: Account locked (timed lock after failed logins, or admin lock)
- `failed_login_attempts`: Counter (reset on successful login)
- `locked_until`: Unlock timestamp of a timed lock; NULL with `is_locked` means an indefinite admin lock

### Session Model

```python
class Session(Base):
    __tablename__ = "sessions"
    
    id: UUID                      # Session ID
    user_id: UUID                 # User who owns session
    token_hash: str               # SHA-256 hash of JWT
    family_id: UUID               # Token rotation family
    is_revoked: bool              # Explicitly revoked?
    expires_at: datetime           # When session expires
    device_id: str                # Device identifier
    device_name: str              # "iPhone 14"
    ip_address: str               # Client IP
    user_agent: str               # Browser info
```

### Email Token Model

```python
class EmailToken(Base):
    __tablename__ = "email_tokens"
    
    id: UUID                      # Token ID
    user_id: UUID                 # User who owns token
    purpose: str                  # VERIFY_EMAIL, RESET_PASSWORD
    token_hash: str               # SHA-256 hash
    new_email: str                # For email change
    expires_at: datetime           # Token expiration
    used_at: datetime              # When used (null if unused)
    invalidated_at: datetime       # Manual invalidation
```

## JWT Token Structure

### Token Payload

```json
{
  "sub": "550e8400-e29b-41d4-a716-446655440000",  // User ID
  "aud": "api",                                      // Audience
  "exp": 1709107200,                                 // Expiration time
  "iat": 1709106900,                                 // Issued at
  "type": "access"                                   // "access" or "refresh"
}
```

### Token Expiration Times

- **Access token**: 15 minutes (short-lived)
- **Refresh token**: 30 days (long-lived)

### Token Types

- **access**: Use for API calls to authenticate request
- **refresh**: Use to obtain new access token when expired

## Endpoint Reference

### `POST /api/auth/register`

Register new user account.

**Request**:
```json
{
  "username": "john_doe",
  "email": "john@example.com",
  "password": "SecurePassword123!"
}
```

**Response** (201 Created):
```json
{
  "id": "550e8400-e29b-41d4-a716-446655440000",
  "username": "john_doe",
  "email": "john@example.com",
  "is_active": true,
  "is_email_verified": false,
  "created_at": "2026-02-27T10:00:00Z"
}
```

**Errors**:
- 400: Username already taken
- 400: Email already registered
- 422: Invalid email format

### `POST /api/auth/verify-email`

Verify user email address.

**Query**:
```
GET /api/auth/verify-email?token=encrypted-token
```

**Response** (200 OK):
```json
{
  "success": true,
  "message": "Email verified successfully"
}
```

**Errors**:
- 400: Invalid or expired token
- 400: Token already used

### `POST /api/auth/login`

Authenticate user and get access/refresh tokens.

**Request**:
```json
{
  "username": "john_doe",
  "password": "SecurePassword123!",
  "device_id": "device-uuid",
  "device_name": "iPhone 14"
}
```

**Response** (200 OK):
```json
{
  "access_token": "eyJhbGciOiJIUzI1NiIs...",
  "refresh_token": "eyJhbGciOiJIUzI1NiIs...",
  "token_type": "bearer",
  "expires_in": 900
}
```

**Errors**:
- 401: Invalid username or password
- 423: Account locked (too many failed attempts)
- 401: Email not verified (registration incomplete)

### `POST /api/auth/refresh`

Get new access token using refresh token.

**Request**:
```json
{
  "refresh_token": "eyJhbGciOiJIUzI1NiIs..."
}
```

**Response** (200 OK):
```json
{
  "access_token": "eyJhbGciOiJIUzI1NiIs...",
  "refresh_token": "eyJhbGciOiJIUzI1NiIs...",
  "token_type": "bearer",
  "expires_in": 900
}
```

**Errors**:
- 401: Refresh token invalid or expired
- 401: Session revoked

### `POST /api/auth/logout`

Revoke current session.

**Request**:
```
Authorization: Bearer access_token
```

**Response** (200 OK):
```json
{
  "success": true,
  "message": "Logged out successfully"
}
```

### `POST /api/auth/forgot-password`

Request password reset token.

**Request**:
```json
{
  "email": "john@example.com"
}
```

**Response** (200 OK):
```json
{
  "success": true,
  "message": "Password reset email sent"
}
```

Note: Always returns success (don't reveal if email exists).

### `POST /api/auth/reset-password`

Reset password using token.

**Request**:
```json
{
  "token": "encrypted-token",
  "new_password": "NewSecurePassword123!"
}
```

**Response** (200 OK):
```json
{
  "success": true,
  "message": "Password reset successfully"
}
```

**Errors**:
- 400: Invalid or expired token
- 400: Token already used

## Using JWT in Requests

All authenticated endpoints require Bearer token in Authorization header:

```bash
curl -H "Authorization: Bearer access_token" \
  https://api.example.com/api/user
```

In Python (httpx/requests):

```python
headers = {"Authorization": f"Bearer {access_token}"}
response = httpx.get("https://api.example.com/api/user", headers=headers)
```

## Implementation Details

### Password Hashing

Uses **Argon2** (OWASP-recommended):

```python
from argon2 import PasswordHasher

hasher = PasswordHasher()
hash = hasher.hash("SecurePassword123!")  # ~12-15 rounds by default
hasher.verify(hash, "SecurePassword123!")  # True
hasher.verify(hash, "WrongPassword")       # Raises VerifyMismatchError
```

Settings in [src/common/config.py](../src/common/config.py):
```python
ARGON2_TIME_COST=3          # default
ARGON2_MEMORY_KIB=65536     # default (64 MiB)
ARGON2_PARALLELISM=4        # default
PASSWORD_HASH_CONCURRENCY=4 # max concurrent hashes per process
# BCRYPT_ROUNDS was removed; it never applied to Argon2.
# Hashing runs in a thread pool; hashes with old parameters are rehashed on login.
```

### Token Hashing

Tokens are hashed before storage (prevent token DB leak):

```python
def hash_token(raw_token: str) -> str:
    return hashlib.sha256(raw_token.encode()).hexdigest()

# Generate token
raw_token = secrets.token_urlsafe(32)
token_hash = hash_token(raw_token)
db.session.token_hash = token_hash  # Store hash

# Later, verify token
incoming_hash = hash_token(received_token)
session = db.get(Session).filter_by(token_hash=incoming_hash).first()
```

### Token Rotation & Replay Detection

Refresh tokens are opaque random strings, stored only as SHA-256 hashes in the `sessions` table. Redis holds no refresh tokens.

```
Login
├─ Create a session row with family_id=F
└─ Issue access token (claim sid=F) and an opaque refresh token

Refresh (valid token)
├─ Atomic UPDATE ... RETURNING marks the session row rotated
│  (only matches while not revoked and not expired)
└─ Issue a new pair in the same family F

Reuse (token already rotated or revoked)
├─ The UPDATE matches no row
├─ Revoke every session in family F
└─ The client must log in again
   (two concurrent refreshes of the same token count as reuse)

Refresh for an inactive or admin-locked user
└─ Denied, family revoked
   (a timed lock from failed logins does not deny refresh)

Concurrency
└─ Refresh and bulk revocations take a row lock on users (FOR NO KEY UPDATE) first,
   so a revocation always sees the successor session of a parallel refresh
   (lock order: email_tokens → users → sessions)
```

### Session Revocation

| Event | Effect |
|---|---|
| `POST /auth/logout` | revokes the current session |
| `POST /auth/logout-all` (bearer) | revokes all sessions of the user and clears the cookie |
| Password reset | revokes all sessions |
| Password change | revokes all sessions except the caller's (`sid` claim) |
| Reuse of a rotated refresh token | revokes the whole family |

Access tokens already issued stay valid until they expire (15 minutes), except for admin-locked users, who get 401.

### Brute-Force Protection

There are two kinds of lock. See [README.md → Authentication design](../README.md#authentication-design) for the full comparison.

- **Timed lock:** set after `MAXIMUM_LOGIN_ATTEMPTS` (5) failed logins, for `LOGIN_ATTEMPT_WINDOW_SECONDS` (900 s, `src/common/constants.py`). It blocks only password login. Existing sessions, access tokens and refresh stay valid, so a stranger cannot log a user out by guessing passwords.
- **Admin lock:** `is_locked` with `locked_until` NULL. Access tokens get 401, refresh is denied, and the admin action "Lock selected" revokes all sessions.

```python
# Login failure (one atomic UPDATE in crud.register_failed_login)
user.failed_login_attempts += 1
if user.failed_login_attempts >= MAXIMUM_LOGIN_ATTEMPTS:
    user.is_locked = True
    user.locked_until = now() + timedelta(seconds=LOGIN_ATTEMPT_WINDOW_SECONDS)

# Login success (one conditional UPDATE in crud.record_successful_login)
# applies only if no lock is active:
#   is_locked and (locked_until is NULL or locked_until > now())
user.failed_login_attempts = 0
user.is_locked = False
user.locked_until = None
```

Per process, at most `PASSWORD_HASH_CONCURRENCY` password evaluations run at once, and the lock is re-checked after admission. This bounds a login burst but does not replace rate limiting (open decision D4).

## Security Best Practices

1. **Never log tokens**: Sensitive data
2. **Always use HTTPS**: Tokens transmitted in plain text
3. **Use secure cookies**: For web clients, store tokens in httpOnly cookies
4. **Implement rate limiting**: Prevent brute force on login endpoint
5. **Validate email** on registration: Prevent spam/typos
6. **Refresh token rotation**: Get new tokens on each refresh
7. **Short access token life**: Minimize window if token stolen
8. **Revoke on suspicious activity**: Detect and respond to attacks

## Audit Logging

All authentication events are logged for security monitoring:

```python
# In auth service
logger.info(
    "auth.login_success",
    user_id=user.id,
    ip_address=request.client.host,
    user_agent=request.headers.get("user-agent"),
    device_id=device_id,
)

logger.warning(
    "auth.login_failed",
    username=username,
    reason="invalid_password",
    attempt=user.failed_login_attempts,
    ip_address=request.client.host,
)

logger.info(
    "auth.account_locked",
    user_id=user.id,
    ip_address=request.client.host,
    locked_until=user.locked_until,
)
```

---

**Related Documentation:**
- See [database_schemas/user.md](database_schemas/user.md) for auth schema
- See [redis.md](redis.md) for session caching
- See [logging.md](logging.md) for auth event logging
