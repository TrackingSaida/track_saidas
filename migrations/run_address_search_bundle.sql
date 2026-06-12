-- Bundle: busca inteligente de endereços (mobile)
-- Executar no Postgres de produção após deploy com PR #19.
-- Idempotente: cada arquivo usa IF NOT EXISTS onde aplicável.

\i geocode_cache.sql
\i suggestion_cache.sql
\i enderecos_conhecidos.sql
\i address_telemetry.sql
\i geocode_metadata.sql
\i coord_precision.sql
