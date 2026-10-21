-- Job issuers_static (WEB-13 HS1) reads MOEX ISS emitter cards.
INSERT INTO ops.job_source (job, source) VALUES ('issuers_static', 'moex_iss')
ON CONFLICT DO NOTHING;
