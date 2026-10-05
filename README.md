# SnailStamp

Enterprise-grade Django project with tamper-evident item ledger and multi-tenant associations.

## Quick Start

This project uses [uv](https://github.com/astral-sh/uv) as the package manager for fast dependency management.

### Prerequisites

- Python 3.12 or higher
- [uv](https://github.com/astral-sh/uv) installed:
  ```bash
  curl -LsSf https://astral.sh/uv/install.sh | sh
  # or with brew: brew install uv
  ```

### Installation

1. Install dependencies:
   ```bash
   uv sync
   ```

2. Install development dependencies (optional, for testing/linting):
   ```bash
   uv sync --extra dev
   ```

3. Configure environment:
   ```bash
   cp .env.example .env
   # Edit .env with your configuration
   ```

4. Run migrations:
   ```bash
   uv run python src/snailstamp/manage.py migrate
   ```

5. Create a superuser:
   ```bash
   uv run python src/snailstamp/manage.py createsuperuser
   ```

6. Run the development server:
   ```bash
   uv run python src/snailstamp/manage.py runserver
   ```

## Common Commands

```bash
# Run Django management commands
uv run python src/snailstamp/manage.py <command>

# Examples:
uv run python src/snailstamp/manage.py runserver
uv run python src/snailstamp/manage.py migrate
uv run python src/snailstamp/manage.py createsuperuser
uv run python src/snailstamp/manage.py check
uv run python src/snailstamp/manage.py shell

# Run tests (if dev dependencies installed)
uv run pytest

# Run linter (if dev dependencies installed)
uv run ruff check .
uv run black .

# Install new dependency
uv add <package-name>

# Install dev dependency
uv add --dev <package-name>
```

## Documentation

- **[API.md](API.md)** - REST API documentation and endpoints
- **[AUTHENTICATION.md](AUTHENTICATION.md)** - Authentication setup (email/password, Google OAuth)
- **[ASSET_MANAGEMENT.md](ASSET_MANAGEMENT.md)** - Asset management guide
- **[BLOCK_SIGNING.md](BLOCK_SIGNING.md)** - Block signing and integrity
- **[STATE_SNAPSHOT.md](STATE_SNAPSHOT.md)** - State snapshot documentation

## API Documentation

REST API v1 menggunakan Django REST Framework. OpenAPI tersedia pada `/api/v1/schema/` dan Swagger UI pada `/api/docs/`.

## Architecture

- **Multi-tenant**: Associations with member-based access control
- **Tamper-evident ledger**: Append-only transaction history with cryptographic signatures
- **Async processing**: Celery + Redis for transaction queue
- **Storage**: Local (development) or S3 (production)
- **Authentication**: Email/password and Google OAuth via django-allauth
