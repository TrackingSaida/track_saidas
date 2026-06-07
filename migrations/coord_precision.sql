-- Precisão das coordenadas salvas no endereço da entrega (rooftop | street | approx)
ALTER TABLE saidas_detail ADD COLUMN IF NOT EXISTS coord_precision TEXT NULL;
