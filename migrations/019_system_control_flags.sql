-- Migration 019: System control flags (master switch, paper mode)

CREATE TABLE IF NOT EXISTS system_control_flags (
    flag_name   TEXT        PRIMARY KEY,
    flag_value  BOOLEAN     NOT NULL,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_by  TEXT        NOT NULL DEFAULT 'system'
);

INSERT INTO system_control_flags (flag_name, flag_value, updated_by) VALUES
    ('master_switch', TRUE,  'system'),
    ('paper_mode',    TRUE,  'system')
ON CONFLICT (flag_name) DO NOTHING;
