-- Production has one cognitive authority. Existing contexts are promoted with
-- runtime epoch/lease fencing; historical RP scenes remain retired.
ALTER TABLE cognitive_contexts ALTER COLUMN authority SET DEFAULT 'active';
