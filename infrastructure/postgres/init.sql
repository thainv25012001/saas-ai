-- Runs once, as superuser, against the saas_ai database.

CREATE EXTENSION IF NOT EXISTS vector;    -- installed now, first used in Phase 3
CREATE EXTENSION IF NOT EXISTS citext;    -- case-insensitive email
CREATE EXTENSION IF NOT EXISTS pgcrypto;  -- gen_random_bytes for API keys

-- app_owner owns every table and runs migrations. As table owner it bypasses
-- RLS, which is what lets migrations and seeds work unconditionally.
CREATE ROLE app_owner WITH LOGIN PASSWORD 'app_owner_password' NOBYPASSRLS;

-- app_user is what the application connects as. It is NOT the table owner,
-- so RLS policies apply to it. This separation is the whole point.
CREATE ROLE app_user WITH LOGIN PASSWORD 'app_user_password' NOBYPASSRLS;

GRANT CONNECT ON DATABASE saas_ai TO app_owner, app_user;
GRANT USAGE ON SCHEMA public TO app_owner, app_user;
GRANT CREATE ON SCHEMA public TO app_owner;

-- Anything app_owner creates later is automatically usable by app_user.
ALTER DEFAULT PRIVILEGES FOR ROLE app_owner IN SCHEMA public
  GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO app_user;
ALTER DEFAULT PRIVILEGES FOR ROLE app_owner IN SCHEMA public
  GRANT USAGE, SELECT ON SEQUENCES TO app_user;
