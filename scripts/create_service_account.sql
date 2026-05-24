-- =============================================================================
-- Kalshi Bot — Azure SQL Service Account Setup
-- Run this ONCE as your Azure SQL admin user (e.g. via SSMS or Azure Portal query editor)
-- Replace <YOUR_STRONG_PASSWORD> with a generated password before running
-- =============================================================================

-- 1. Create a contained database user (no server-level login needed for Azure SQL)
CREATE USER kalshi_bot WITH PASSWORD = '<YOUR_STRONG_PASSWORD>';

-- 2. Data access — read/write on all bot tables
ALTER ROLE db_datareader ADD MEMBER kalshi_bot;
ALTER ROLE db_datawriter ADD MEMBER kalshi_bot;

-- 3. Schema management — needed for init_db() on first startup
--    (creates tables, adds columns via ALTER TABLE, creates indexes)
ALTER ROLE db_ddladmin ADD MEMBER kalshi_bot;

-- 4. DB size query in maintenance.py uses sys.database_files
GRANT VIEW DATABASE STATE TO kalshi_bot;

-- =============================================================================
-- VERIFY the account works (run as kalshi_bot or check in SSMS)
-- =============================================================================
-- SELECT USER_NAME();                          -- should return 'kalshi_bot'
-- SELECT TOP 1 * FROM INFORMATION_SCHEMA.TABLES;  -- should list tables

-- =============================================================================
-- CONNECTION STRING for .env (swap in your server/db/password)
-- =============================================================================
-- AZURE_SQL_CONN_STR=Driver={ODBC Driver 18 for SQL Server};
--   Server=tcp:<your-server>.database.windows.net,1433;
--   Database=<your-database>;
--   Uid=kalshi_bot;
--   Pwd=<YOUR_STRONG_PASSWORD>;
--   Encrypt=yes;
--   TrustServerCertificate=no;
--   Connection Timeout=30;

-- =============================================================================
-- TO REVOKE DDL rights after first run (optional — tighten permissions later)
-- =============================================================================
-- ALTER ROLE db_ddladmin DROP MEMBER kalshi_bot;
