# Authentication & Social Login

SnailStamp supports multiple authentication methods using django-allauth and dj-rest-auth:

- **Email/Password**: Traditional registration and login with email address
- **Google OAuth**: Social login with Google account
- **JWT Tokens**: API authentication using JWT access/refresh tokens

## Configuration

### Environment Variables

Configure the following in your `.env` file:

```bash
# Email Configuration (for email verification)
EMAIL_BACKEND=django.core.mail.backends.console.EmailBackend  # Use console for development
EMAIL_HOST=smtp.gmail.com
EMAIL_PORT=587
EMAIL_USE_TLS=True
EMAIL_HOST_USER=your-email@gmail.com
EMAIL_HOST_PASSWORD=your-app-password
DEFAULT_FROM_EMAIL=noreply@snailstamp.com

# django-allauth Configuration
ACCOUNT_EMAIL_VERIFICATION=mandatory  # or 'none' to disable
SOCIALACCOUNT_EMAIL_VERIFICATION=none  # OAuth users are verified by provider
```

### Django Admin Setup

After running migrations, configure social providers via Django Admin:

1. Navigate to `/admin/`
2. Go to **Sites** and update the default site:
   - Domain: `localhost` (for development) or your production domain
   - Display name: Your site name
3. Go to **Social Applications** and add a new application:
   - **Provider**: Google
   - **Name**: Google OAuth
   - **Client ID**: (from Google Cloud Console)
   - **Secret Key**: (from Google Cloud Console)
   - **Key**: (same as Client ID)
   - **Sites**: Select your site from the list

### Google OAuth Setup

1. Create a project at [Google Cloud Console](https://console.cloud.google.com/)
2. Enable Google+ API:
   - Go to APIs & Services > Library
   - Search for "Google+ API" and enable it
3. Create OAuth 2.0 credentials:
   - Go to APIs & Services > Credentials
   - Create credentials > OAuth client ID
   - Application type: Web application
   - Authorized redirect URIs:
     - Development: `http://localhost:8000/api/v1/auth/google/callback/`
     - Production: `https://yourdomain.com/api/v1/auth/google/callback/`
4. Copy the Client ID and Client Secret to Django Admin

## API Endpoints

### Registration (Email/Password)

Register a new user with email and password:

```http
POST /api/v1/auth/registration/
Content-Type: application/json

{
  "email": "user@example.com",
  "password1": "securepassword123",
  "password2": "securepassword123"
}
```

**Response:**

```json
{
  "key": "jwt-access-token",
  "user": {
    "id": "uuid",
    "email": "user@example.com",
    "first_name": "",
    "last_name": ""
  }
}
```

If `ACCOUNT_EMAIL_VERIFICATION=mandatory`, the user will receive a verification email and must click the link before logging in.

### Login (Email/Password)

Authenticate with email and password:

```http
POST /api/v1/auth/login/
Content-Type: application/json

{
  "email": "user@example.com",
  "password": "securepassword123"
}
```

**Response:**

```json
{
  "key": "jwt-access-token",
  "user": {
    "id": "uuid",
    "email": "user@example.com",
    "first_name": "",
    "last_name": ""
  }
}
```

### Google OAuth Flow

#### Option 1: Browser-Based Flow

Redirect user to Google for authentication:

```http
GET /accounts/google/login/?process=login
```

After successful authentication with Google, the user will be redirected back to the callback URL and logged in automatically.

#### Option 2: API-Based Flow (for mobile/spa)

Use the dj-rest-auth social login endpoint:

```http
POST /api/v1/auth/google/
Content-Type: application/json

{
  "access_token": "google-oauth-access-token"
}
```

**Response:**

```json
{
  "key": "jwt-access-token",
  "user": {
    "id": "uuid",
    "email": "user@example.com",
    "first_name": "John",
    "last_name": "Doe"
  }
}
```

### Token Management

Get current user info:

```http
GET /api/v1/auth/user/
Authorization: Bearer <jwt-access-token>
```

**Response:**

```json
{
  "id": "uuid",
  "email": "user@example.com",
  "first_name": "John",
  "last_name": "Doe"
}
```

Logout (invalidates the current token):

```http
POST /api/v1/auth/logout/
Authorization: Bearer <jwt-access-token>
```

### JWT Token Refresh

Refresh an expired access token:

```http
POST /api/v1/auth/token/refresh/
Content-Type: application/json

{
  "refresh": "jwt-refresh-token"
}
```

**Response:**

```json
{
  "access": "new-jwt-access-token"
}
```

## Using JWT Tokens

Include the JWT access token in the `Authorization` header for protected endpoints:

```http
GET /api/v1/me/associations/
Authorization: Bearer <jwt-access-token>
```

## Email Verification

When `ACCOUNT_EMAIL_VERIFICATION=mandatory`:

1. User registers via `/api/v1/auth/registration/`
2. System sends verification email to the user's email address
3. User clicks the verification link in the email
4. Account is verified and user can login

For development with console email backend, the verification link will be printed to the console where the server is running.

## Security Notes

- Always use HTTPS in production
- Store OAuth credentials securely (use environment variables)
- Rotate OAuth secrets regularly
- Enable email verification to prevent fake accounts
- Google OAuth users are automatically verified (set `SOCIALACCOUNT_EMAIL_VERIFICATION=none`)
- Email/password users require verification if `ACCOUNT_EMAIL_VERIFICATION=mandatory`

## Troubleshooting

### Google OAuth Not Working

- Verify redirect URI matches exactly (including trailing slash)
- Check that the Site domain in Django Admin matches your request domain
- Ensure Google+ API is enabled in Google Cloud Console
- Check that Social Application is configured with correct Client ID and Secret

### Email Not Sending

- Verify SMTP credentials in `.env`
- For Gmail, use an App Password (not your regular password)
- Check that `EMAIL_HOST_USER` and `EMAIL_HOST_PASSWORD` are set
- For development, use `EMAIL_BACKEND=django.core.mail.backends.console.EmailBackend`

### Registration/Login Fails

- Check that migrations have been run: `python manage.py migrate`
- Verify `ACCOUNT_EMAIL_VERIFICATION` setting
- Ensure custom adapters are configured in settings
- Check Django logs for detailed error messages

### SignUp with passkey

- [POST] http://localhost:8000/api/allauth/app/v1/auth/webauthn/signup - start email validation, user will receive 8 validation code (must place X-Session-Token in header)
- [POST] http://localhost:8000/api/allauth/app/v1/auth/email/verify - verify email with validation code (must place X-Session-Token in header, from prev step)
- [GET] http://localhost:8000/api/allauth/app/v1/auth/webauthn/signup - generate passkey use webauth method (must place X-Session-Token in header, from prev step)
- [PUT] http://localhost:8000/api/allauth/app/v1/auth/webauthn/signup - create access token (must place X-Session-Token in header, from prev step)
