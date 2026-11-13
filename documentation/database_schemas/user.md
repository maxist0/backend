# Database Schemas

## Users Table

The central user table for authentication and user management.

```sql
CREATE TABLE users (
    id UUID PRIMARY KEY,
    username VARCHAR(64) UNIQUE NOT NULL,
    email VARCHAR(255) UNIQUE NOT NULL,
    display_name VARCHAR(128) DEFAULT NULL,
    is_active BOOLEAN DEFAULT TRUE NOT NULL,
    is_email_verified BOOLEAN DEFAULT FALSE NOT NULL,
    is_locked BOOLEAN DEFAULT FALSE NOT NULL,
    failed_login_attempts INTEGER DEFAULT 0 NOT NULL,
    locked_until TIMESTAMP WITH TIME ZONE DEFAULT NULL,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL,
    updated_at TIMESTAMP WITH TIME ZONE NOT NULL,
    last_login_at TIMESTAMP WITH TIME ZONE DEFAULT NULL
);
```

### Fields Description

| Field | Type | Nullable | Unique | Description |
|-------|------|----------|--------|-------------|
| `id` | UUID | No | Yes | Primary key, auto-generated |
| `username` | VARCHAR(64) | No | Yes | Login identifier |
| `email` | VARCHAR(255) | No | Yes | Contact email |
| `display_name` | VARCHAR(128) | Yes | No | User's display name |
| `is_active` | BOOLEAN | No | No | Whether account is active |
| `is_email_verified` | BOOLEAN | No | No | Email verification status |
| `is_locked` | BOOLEAN | No | No | Account locked (brute force protection) |
| `failed_login_attempts` | INTEGER | No | No | Count of failed logins (reset on success) |
| `locked_until` | TIMESTAMP TZ | Yes | No | Unlock timestamp if locked |
| `created_at` | TIMESTAMP TZ | No | No | Account creation time |
| `updated_at` | TIMESTAMP TZ | No | No | Last modification time |
| `last_login_at` | TIMESTAMP TZ | Yes | No | Last successful login |

### Relationships

- **One-to-Many**: User → Sessions (user can have multiple active sessions)
- **One-to-Many**: User → EmailTokens (user can have multiple email verification/reset tokens)
- **One-to-Many**: User → AuthAuditLog (user login/registration events)
- **One-to-One**: User → PasswordCredential (user has exactly one password hash)

### Constraints

- `UNIQUE(username)`: Username must be unique
- `UNIQUE(email)`: Email must be unique
- `CHECK(is_active IN (true, false))`: Data integrity
- All timestamps are timezone-aware UTC

---

## Password Credentials Table

Stores hashed passwords separately from user data for security.

```sql
CREATE TABLE password_credentials (
    id UUID PRIMARY KEY,
    user_id UUID UNIQUE NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    password_hash VARCHAR(512) NOT NULL,
    must_change BOOLEAN DEFAULT FALSE NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL,
    updated_at TIMESTAMP WITH TIME ZONE NOT NULL
);
```

### Fields Description

| Field | Type | Description |
|-------|------|-------------|
| `id` | UUID | Primary key |
| `user_id` | UUID (FK) | Reference to users.id (unique) |
| `password_hash` | VARCHAR(512) | Argon2 hashed password (never plain text) |
| `must_change` | BOOLEAN | Force password change on next login |
| `created_at` | TIMESTAMP TZ | When password was last set |
| `updated_at` | TIMESTAMP TZ | When password was last updated |

### Design Decisions

- **Separate table**: If password_credentials are compromised, user account data remains secure
- **UNIQUE constraint on user_id**: Each user has exactly one active password
- **ON DELETE CASCADE**: When user deleted, password also deleted
- **must_change flag**: Admin can force password reset without knowing old password

---

## Sessions Table

Multi-session support with token rotation and device tracking.

```sql
CREATE TABLE sessions (
    id UUID PRIMARY KEY,
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    token_hash VARCHAR(64) UNIQUE NOT NULL,
    family_id UUID NOT NULL,
    is_revoked BOOLEAN DEFAULT FALSE NOT NULL,
    revoke_reason VARCHAR(64) DEFAULT NULL,
    expires_at TIMESTAMP WITH TIME ZONE NOT NULL,
    device_id VARCHAR(255) DEFAULT NULL,
    device_name VARCHAR(255) DEFAULT NULL,
    ip_address VARCHAR(45) DEFAULT NULL,
    user_agent VARCHAR(512) DEFAULT NULL,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL,
    revoked_at TIMESTAMP WITH TIME ZONE DEFAULT NULL,
    UNIQUE(token_hash),
    FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE,
    INDEX(user_id),
    INDEX(family_id)
);
```

### Fields Description

| Field | Type | Description |
|-------|------|-------------|
| `id` | UUID | Session ID |
| `user_id` | UUID (FK) | User who owns this session |
| `token_hash` | VARCHAR(64) | SHA-256 hash of JWT token (never store raw token) |
| `family_id` | UUID | Token family for rotation detection (prevent replay attacks) |
| `is_revoked` | BOOLEAN | Whether session is explicitly revoked |
| `revoke_reason` | VARCHAR(64) | Why revoked (e.g., "logout", "suspicious_activity") |
| `expires_at` | TIMESTAMP TZ | When session expires |
| `device_id` | VARCHAR(255) | Unique device identifier |
| `device_name` | VARCHAR(255) | Human-readable device name |
| `ip_address` | VARCHAR(45) | IPv4 or IPv6 address (max 45 chars) |
| `user_agent` | VARCHAR(512) | Browser/client user agent |
| `created_at` | TIMESTAMP TZ | Session creation time |
| `revoked_at` | TIMESTAMP TZ | When session was revoked |

### Key Concepts

**Token Family**: Prevents refresh token replay attacks
- When refresh token is used, new token family is created
- Detects if old refresh token is replayed → immediate revocation
- All sessions in family are revoked if replay detected

**Device Tracking**: Track login locations and devices
- Detect suspicious logins from new devices
- User can see active sessions and revoke by device
- Example: "iPhone 14, New York, Last 5 minutes"

### Indexes

- `user_id`: Quickly find all sessions for a user
- `family_id`: Find tokens in same family (for rotation/revocation)
- `token_hash`: Fast token lookup for validation

---

## Email Tokens Table

Manages email verification, email change, and password reset tokens.

```sql
CREATE TABLE email_tokens (
    id UUID PRIMARY KEY,
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    purpose VARCHAR(64) NOT NULL,  -- VERIFY_EMAIL, CHANGE_EMAIL, RESET_PASSWORD
    token_hash VARCHAR(64) NOT NULL,
    new_email VARCHAR(255) DEFAULT NULL,
    expires_at TIMESTAMP WITH TIME ZONE NOT NULL,
    used_at TIMESTAMP WITH TIME ZONE DEFAULT NULL,
    invalidated_at TIMESTAMP WITH TIME ZONE DEFAULT NULL,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL,
    UNIQUE(token_hash),
    INDEX(user_id, purpose),
    INDEX(expires_at)
);
```

### Fields Description

| Field | Type | Description |
|-------|------|-------------|
| `id` | UUID | Token ID |
| `user_id` | UUID (FK) | User who requested this token |
| `purpose` | VARCHAR(64) | Token purpose (VERIFY_EMAIL, CHANGE_EMAIL, RESET_PASSWORD) |
| `token_hash` | VARCHAR(64) | SHA-256 hash of token (never store raw token) |
| `new_email` | VARCHAR(255) | For CHANGE_EMAIL: the new email address to verify |
| `expires_at` | TIMESTAMP TZ | When token expires (typically 24 hours) |
| `used_at` | TIMESTAMP TZ | When token was successfully used (NULL = unused) |
| `invalidated_at` | TIMESTAMP TZ | When token was manually invalidated (NULL = active) |
| `created_at` | TIMESTAMP TZ | Token creation time |

### Token Purposes

- **VERIFY_EMAIL**: Email verification after registration
- **CHANGE_EMAIL**: Verify new email before changing account email
- **RESET_PASSWORD**: Reset password via email link

### Lifecycle

1. Token created with random UUID, hashed, stored
2. Token sent to user's email (link contains raw token)
3. User clicks link with token
4. Server verifies token hash matches, not expired, not invalidated
5. If used successfully → set `used_at` and action completes
6. If token expires → user gets new token, old token remains in DB for audit trail

### Indexes

- `user_id, purpose`: Find active tokens for user (e.g., pending email change)
- `expires_at`: Clean up expired tokens regularly

---

## Auth Audit Log Table

Records authentication events for security auditing and troubleshooting.

```sql
CREATE TABLE auth_audit_log (
    id UUID PRIMARY KEY,
    user_id UUID DEFAULT NULL REFERENCES users(id) ON DELETE SET NULL,
    event_type VARCHAR(64) NOT NULL,  -- REGISTER, LOGIN_SUCCESS, LOGIN_FAILED, etc.
    ip_address VARCHAR(45) DEFAULT NULL,
    user_agent VARCHAR(512) DEFAULT NULL,
    detail TEXT DEFAULT NULL,  -- JSON-encoded additional info
    created_at TIMESTAMP WITH TIME ZONE NOT NULL,
    INDEX(user_id),
    INDEX(event_type),
    INDEX(created_at)
);
```

### Fields Description

| Field | Type | Description |
|-------|------|-------------|
| `id` | UUID | Log entry ID |
| `user_id` | UUID (FK) | Associated user (NULL for registration events) |
| `event_type` | VARCHAR(64) | Type of auth event |
| `ip_address` | VARCHAR(45) | Client IP address |
| `user_agent` | VARCHAR(512) | Browser/client identifying string |
| `detail` | TEXT | JSON containing event-specific details |
| `created_at` | TIMESTAMP TZ | Event timestamp |

### Event Types

```
REGISTER                    -- New user registration
LOGIN_SUCCESS              -- Successful password login
LOGIN_FAILED               -- Failed password login (invalid credentials)
ACCOUNT_LOCKED             -- Account locked due to failed attempts
ACCOUNT_UNLOCKED           -- Account manually unlocked by admin
EMAIL_VERIFICATION_SENT    -- Verification token sent
EMAIL_VERIFIED             -- Email successfully verified
EMAIL_CHANGED              -- User changed email address
PASSWORD_CHANGED           -- User changed password
PASSWORD_RESET_REQUESTED   -- Password reset requested
PASSWORD_RESET_COMPLETED   -- Password reset completed
SESSION_REVOKED            -- Session explicitly revoked
```

### Detail Field Examples

```json
{
  "failed_attempts": 3,
  "reason": "invalid_password"
}
```

```json
{
  "old_email": "john@old.com",
  "new_email": "john@new.com",
  "verification_sent": true
}
```

```json
{
  "revoke_reason": "suspicious_activity",
  "device": "iPhone 14",
  "location": "New York, USA"
}
```

### Use Cases

- **Security monitoring**: Detect brute force attacks
- **Compliance**: Audit trail for regulatory requirements
- **User support**: Check login history
- **Threat detection**: Identify suspicious patterns

### Indexes

- `user_id`: Find all events for a user
- `event_type`: Find specific event types
- `created_at`: Query events by time range

---

## Schema Relationships Diagram

```
users (1)
├── (1) password_credentials
├── (N) sessions
├── (N) email_tokens
└── (N) auth_audit_log
```

Only users table, password_credentials, and relationships are implemented in the initial migration.
Email tokens and audit logging will be added in future releases.

---

## Data Integrity Constraints

### Foreign Keys

All foreign keys use `ON DELETE CASCADE` where appropriate:
- Delete user → cascades delete sessions, email_tokens, password_credentials
- Delete user → auth_audit_logs set user_id to NULL (preserve audit trail)

### Unique Constraints

- `users.username`: Prevent duplicate usernames
- `users.email`: Prevent user accounts with same email
- `password_credentials.user_id`: One password per user
- `sessions.token_hash`: Each token is unique
- `email_tokens.token_hash`: Each token is unique

### Check Constraints

- `is_active IN (true, false)`: Boolean values only
- `is_locked IN (true, false)`: Boolean values only

---

**Related Documentation:**
- See [database.md](database.md) for query patterns and ORM usage
- See [authentication.md](authentication.md) for auth flow using these schemas
