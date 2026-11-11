-- Record the owner's decision on reading the SPB Exchange public website (2026-11-11).
UPDATE ops.source
   SET licence_note = 'Public website data; exchange terms apply. Reading the public site is '
                      'allowed by the owner (2026-11-11), up to several times a day. '
                      'Chart close is not official close.'
 WHERE code = 'spb';
