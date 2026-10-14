-- llm_intraday_journal: record the ideas validation KILLED, not just the survivors (2026-09-23).
--
-- decision was 'proposed' or 'no_ideas' only, so a pass where the model proposed real
-- trades that our own validation rejected was stored as 'no_ideas' -- indistinguishable
-- from the model looking at the tape and standing aside. Row id=25 is the example: two
-- live ideas (ATGL, CONCOR) killed on reward:risk, filed as if nothing was seen.
--
-- That is not merely a reporting defect. todays_runs() feeds these rows straight back
-- into the next prompt (_summarise_today), so the agent was being told it had declined
-- to trade on passes where it had in fact proposed trades we threw away. It could not
-- learn that its targets were being rejected as too small, and kept proposing the same
-- shape all day. 162 ttl_expired + 34 entry_drift + 6 reward_risk_too_low over one
-- session is what that blind loop looks like from the outside.
--
-- Additive and safe: existing rows default to an empty array.

ALTER TABLE llm_intraday_journal
    ADD COLUMN IF NOT EXISTS rejected_json JSONB NOT NULL DEFAULT '[]'::jsonb;
