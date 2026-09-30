CREATE TABLE IF NOT EXISTS cognitive_event_dependencies (
    context_id BIGINT NOT NULL, event_id BIGINT NOT NULL, source_event_id BIGINT NOT NULL,
    PRIMARY KEY(context_id,event_id,source_event_id),
    FOREIGN KEY(context_id,event_id) REFERENCES cognitive_events(context_id,id),
    FOREIGN KEY(context_id,source_event_id) REFERENCES cognitive_events(context_id,id)
);
