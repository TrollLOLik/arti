-- Offsets refer to the permitted immutable source; chunk text is not duplicated.
ALTER TABLE cognitive_semantic_vectors ADD COLUMN chunk_start INTEGER NOT NULL DEFAULT 0 CHECK(chunk_start>=0);
ALTER TABLE cognitive_semantic_vectors ADD COLUMN chunk_end INTEGER CHECK(chunk_end>chunk_start);
ALTER TABLE cognitive_semantic_vectors DROP CONSTRAINT cognitive_semantic_vectors_pkey;
ALTER TABLE cognitive_semantic_vectors ADD PRIMARY KEY(context_id,artifact_id,embedding_model,chunk_start);
