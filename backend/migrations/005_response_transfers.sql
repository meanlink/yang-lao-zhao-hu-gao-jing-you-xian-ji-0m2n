CREATE TABLE IF NOT EXISTS care_response_transfers (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    response_id bigint NOT NULL REFERENCES care_responses,
    request_key text NOT NULL CHECK (length(btrim(request_key)) BETWEEN 8 AND 100),
    from_staff bigint NOT NULL REFERENCES staff,
    to_staff bigint NOT NULL REFERENCES staff,
    from_coverage_id bigint REFERENCES shift_coverage,
    to_coverage_id bigint NOT NULL REFERENCES shift_coverage,
    reason text NOT NULL CHECK (length(btrim(reason)) BETWEEN 3 AND 600),
    moved_by bigint NOT NULL REFERENCES staff,
    moved_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (request_key),
    CHECK (from_staff <> to_staff)
);
CREATE INDEX IF NOT EXISTS response_transfer_history ON care_response_transfers(response_id, id DESC);
