\set ON_ERROR_STOP on
-- Run once as the provisioning administrator in an empty, dedicated database.
-- Supply passwords through a private environment, never psql command arguments.
\getenv owner_password DIOS_PG_OWNER_PASSWORD
\getenv runtime_password DIOS_PG_RUNTIME_PASSWORD
\getenv backup_password DIOS_PG_BACKUP_PASSWORD
BEGIN;
SET password_encryption = 'scram-sha-256';
CREATE ROLE dios_owner LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION PASSWORD :'owner_password';
CREATE ROLE dios_runtime LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION PASSWORD :'runtime_password';
CREATE ROLE dios_backup LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION PASSWORD :'backup_password';
SELECT format('REVOKE ALL ON DATABASE %I FROM PUBLIC', current_database()) \gexec
SELECT format('GRANT CONNECT ON DATABASE %I TO dios_owner, dios_runtime, dios_backup', current_database()) \gexec
REVOKE ALL ON SCHEMA public FROM PUBLIC;
ALTER SCHEMA public OWNER TO dios_owner;
GRANT USAGE ON SCHEMA public TO dios_runtime, dios_backup;
ALTER DEFAULT PRIVILEGES FOR ROLE dios_owner IN SCHEMA public
  GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO dios_runtime;
ALTER DEFAULT PRIVILEGES FOR ROLE dios_owner IN SCHEMA public
  GRANT USAGE, SELECT ON SEQUENCES TO dios_runtime;
ALTER DEFAULT PRIVILEGES FOR ROLE dios_owner IN SCHEMA public
  GRANT SELECT ON TABLES TO dios_backup;
ALTER DEFAULT PRIVILEGES FOR ROLE dios_owner IN SCHEMA public
  GRANT SELECT ON SEQUENCES TO dios_backup;
COMMIT;
