-- Database roles for a shared Postgres (run once as a superuser; replace the passwords).
--
-- fixloop_owner     owns the schema: runs migrations and is what the services connect as.
--                   Services work across tenants (the worker claims any tenant's task), so
--                   tenant isolation in the services is enforced in code: every query a request
--                   makes is filtered by the authenticated tenant.
-- fixloop_readonly  for dashboards and ad-hoc analysis. Row-level security applies to it:
--                   it sees only the tenant named in the `app.tenant_id` setting, e.g.
--                   SET app.tenant_id = '<tenant uuid>'; before querying.

CREATE ROLE fixloop_owner LOGIN PASSWORD 'change-me';
CREATE DATABASE fixloop OWNER fixloop_owner;

\connect fixloop
CREATE ROLE fixloop_readonly LOGIN PASSWORD 'change-me-too' NOBYPASSRLS;
GRANT CONNECT ON DATABASE fixloop TO fixloop_readonly;
GRANT USAGE ON SCHEMA public TO fixloop_readonly;
ALTER DEFAULT PRIVILEGES FOR ROLE fixloop_owner IN SCHEMA public GRANT SELECT ON TABLES TO fixloop_readonly;
-- After `alembic upgrade head` has created the tables:
-- GRANT SELECT ON ALL TABLES IN SCHEMA public TO fixloop_readonly;
