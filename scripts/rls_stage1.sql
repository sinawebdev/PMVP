-- PMVP v1 — RLS Stage 1 (default-deny the auto-generated PostgREST API)
-- =====================================================================
-- Decision (see MULTI_TENANT.md): tenant isolation is enforced at the
-- application layer (Flask). RLS Stage 1 is a cheap, defense-in-depth
-- backstop: ENABLE (not FORCE) Row Level Security with NO policies on every
-- public table. Effect:
--   * The Flask app connects as role `postgres` (BYPASSRLS + table owner) and
--     is therefore completely unaffected — every query keeps working.
--   * The `anon` / `authenticated` roles used by Supabase client libraries have
--     NO policy, so RLS default-denies them: payroll PII is unreachable via the
--     public REST API even if someone has the publishable/anon key.
-- Stage 2 (JWT-claim tenant policies) is DEFERRED — not needed while the Flask
-- app is the only DB client and app-layer scoping is the guard.
--
-- Precondition (verified before first apply): the app's DB role must bypass RLS.
--   select current_user, rolbypassrls from pg_roles
--     where rolname = current_user;  -- expect postgres / t
--
-- Idempotent: safe to re-run. Run once against a freshly bootstrapped pmvp-v1 DB
-- (already applied to project ejpqjfmnnlgvsqszalmm on first setup).

DO $$
DECLARE t text;
BEGIN
  FOR t IN SELECT tablename FROM pg_tables WHERE schemaname = 'public'
  LOOP
    EXECUTE format('ALTER TABLE public.%I ENABLE ROW LEVEL SECURITY;', t);
  END LOOP;
END $$;

-- Verify:
--   select count(*) filter (where c.relrowsecurity) as rls_on, count(*) as total
--   from pg_tables t join pg_class c on c.relname = t.tablename
--   where t.schemaname = 'public';
