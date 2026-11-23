ALTER TABLE password_reset_tokens DROP COLUMN created_by_id;
ALTER TABLE invitations DROP COLUMN created_by_id;
DROP TABLE auth_attempts;
