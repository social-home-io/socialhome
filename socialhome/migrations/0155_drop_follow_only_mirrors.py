"""0155 — delete the GFS follow-only mirrors of global spaces.

Follows 0153; 0154 is reserved by the open main PR #1430 (moment reach),
which must merge first — ``run_migrations`` never applies a number below
the highest one a database already ran. Re-check before the final PR.

Before v_96 a household followed a global space it was not a member of by
mirroring the space's GFS listing into a local stub
(``GfsSpaceMirrorService.ensure_mirror``) and taking an identity-bound
``POST /gfs/subscribe`` seat; the host sealed it the content key over the
GFS and relayed posts by ``space_id``. From v_96 a follower is a member
with the read-only ``subscriber`` role, seated by the same redeem /
join paths as any member; the mirror machinery and the space-keyed GFS
relay are gone, so a mirror stub can never receive content again and
only shows a dead space in the sidebar. This migration deletes such rows
(owner approval, expected count 0 on real installs).

Only rows that are *provably* a follow-only mirror go — the same evidence
rule the runtime teardown used (``SpaceService._purge_gfs_mirror_if_unused``):

* a ``global`` space owned by another household (``instance_identity``);
* no authority seed held here (``identity_private_key IS NULL``);
* GFS mirror provenance: ``mirror_gfs_id`` (v_44) or a recorded
  ``gfs_space_seats`` row (0092 — which is why this runs before 0156
  drops that table);
* every local seat is a ``subscriber`` one (no owner / admin / member);

and nothing a real seating writes is present: no ``space_instances`` row
(a v_96 redeem / invitation accept / roster snapshot records the host
household there), no ``space_remote_members`` row (roster snapshot), no
``space_invitations`` row (an invitation stub). A database without our
identity row deletes nothing.

Deleting the ``spaces`` row cascades its content graph (posts, comments,
members, keys, …: every space child table is ``ON DELETE CASCADE``); the
media files the deleted rows named are left to the periodic media orphan
sweep (``MediaOrphanSweepService``), which removes files no row references.
Self-contained (no app imports).

CLAUDE.md "audit before a migration":

  (1) Audited: mirror rows were written only by ``ensure_mirror`` (stub +
      ``set_mirror_provenance``) and kept alive by ``gfs_space_seats``;
      both writers are removed in this release. The v_96 follow paths
      (paired join, GFS bootstrap + redeem, follower links) seat through
      ``invite_token_redeem`` / ``private_invite_handler`` /
      ``space_member_directory``, which all add ``space_instances`` /
      ``space_remote_members`` rows — the "spare" conditions above.
  (2) Alternatives rejected: a runtime one-shot purge (permanent code for
      one-time state, run on every boot); leaving the rows (dead spaces in
      the UI that can never update, keyed content kept forever).
  (3) Smallest change: rows only, no shape change; only provable mirrors;
      the columns ``mirror_gfs_id`` / ``gfs_rotation_seq`` stay (inert).
"""

from __future__ import annotations

import sqlite3

_MIRRORS = """
SELECT s.id FROM spaces s
 WHERE s.space_type = 'global'
   AND s.owner_instance_id != ?
   AND s.identity_private_key IS NULL
   AND (s.mirror_gfs_id IS NOT NULL
        OR EXISTS (SELECT 1 FROM gfs_space_seats g WHERE g.space_id = s.id))
   AND NOT EXISTS (SELECT 1 FROM space_members m
                    WHERE m.space_id = s.id AND m.role != 'subscriber')
   AND NOT EXISTS (SELECT 1 FROM space_instances i WHERE i.space_id = s.id)
   AND NOT EXISTS (SELECT 1 FROM space_remote_members r WHERE r.space_id = s.id)
   AND NOT EXISTS (SELECT 1 FROM space_invitations v WHERE v.space_id = s.id)
"""


def migrate(conn: sqlite3.Connection) -> None:
    row = conn.execute("SELECT instance_id FROM instance_identity LIMIT 1").fetchone()
    if row is None or not row[0]:
        return
    for (space_id,) in conn.execute(_MIRRORS, (row[0],)).fetchall():
        conn.execute("DELETE FROM spaces WHERE id=?", (space_id,))
