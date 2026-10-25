# Highlights

Personal "highlights" pillar — a WhatsApp/Instagram-style tap-through
sequence of images and short videos, posted to the author's confirmed
peer instances based on an author-controlled audience.

## Scope

- **HFS**: full participant. Authors post frames, peers receive them
  through the §24.11 inbound pipeline.
- **GFS**: uninvolved. Highlights are personal-scope, not space-scope.

## Event types

`HIGHLIGHT_CREATED`, `HIGHLIGHT_FRAME_APPENDED`, `HIGHLIGHT_FRAME_DELETED`,
`HIGHLIGHT_DELETED`, `HIGHLIGHT_FRAME_VIEWED`, `HIGHLIGHT_FRAME_REACTED`,
`HIGHLIGHT_FRAME_REACTION_REMOVED`.

Plaintext fields on every envelope: `event_type`, `from_instance`,
`to_instance`, `highlight_id`, and (where applicable) `frame_id`. Everything
else — `frame_type`, `sequence`, `media_url`, `caption_text`,
`caption_emoji`, `audience_kind`, `audience`, `expires_at`, viewer /
reactor user ids — rides inside the encrypted payload (§25.8.21).

## Audience kinds

| Kind         | Plaintext addressing                       | `audience` each household receives |
|--------------|--------------------------------------------|------------------------------------|
| `all_paired` | one envelope per `confirmed` peer instance | `[]`                               |
| `households` | one envelope per author-picked peer        | `[<that household>]`               |
| `users`      | one envelope per peer hosting any listed   | only the picked user ids homed on that household — receivers gate their local users on it before surfacing |

**A household learns only its own part of the audience.** The author's
household keeps the full list; `HIGHLIGHT_CREATED` and
`HIGHLIGHT_FRAME_APPENDED` carry a per-household copy trimmed as above
(`HighlightFederationOutbound._audience_per_target`), so no household
learns which other people or households were picked, nor the author's
housemates. Receivers read the list only to gate their own users
(`highlight_repo._IN_AUDIENCE_SQL`; `all_paired` / `households` ignore
it), so the trim needs no protocol version: an older receiver gets
exactly the ids it uses. `HIGHLIGHT_FRAME_DELETED` / `HIGHLIGHT_DELETED`
carry no audience. Release-blocker test:
`tests/protocol/test_highlight_audience_minimization.py`.

## Day model

`UNIQUE(author_user_id, highlight_date)` on the `highlights` table enforces
"one highlight per author per day". The first frame of the day creates the
row + emits `HIGHLIGHT_CREATED`; subsequent frames append + emit
`HIGHLIGHT_FRAME_APPENDED`. New day → new highlight row. The receiving instance
upserts the highlight by id, then appends the frame.

A day's frames share one audience — the highlight row's. The first frame
of the day sets it (the request's `audience_kind` / `audience`, else the
author's Settings default `highlights.default_audience` = `{kind, ids}` in
`preferences_json`; Settings › Privacy › Highlights saves the chosen
household / user ids with the kind, and the SPA composers start a new
day from it and send it explicitly). A later frame
that names an audience must name the same one (same kind, same ids in any
order); otherwise `POST /api/highlights/frames` refuses it with `409
HIGHLIGHT_AUDIENCE_MISMATCH` before anything is written, instead of
showing the frame to today's audience. A frame that names no audience
joins today's highlight; `audience` ids without an `audience_kind` are
refused with `422 VALIDATION_ERROR` (they would be ignored). Two first
frames of the day racing on `UNIQUE(author_user_id, highlight_date)`
don't fail: the loser reads the winner's row and appends (same audience)
or gets the 409.

The composers show today's audience as fixed once the author has posted
today, and **every frame names the audience the composer showed** —
never none. The day is the UTC date, so a frame that lands after UTC
midnight (a slow multi-frame upload, a tab left open overnight) starts a
new day: with the audience it was shown, that day is for the same
people; without one it would take the Settings default (all paired
households out of the box).

"The same people" is the audience. A later frame joins today's
highlight, so it also reaches whoever reads it through a share: a feed or
space post that shared the highlight (`?via_post=` readers see every
frame) and anyone holding its public link (the public stream lists the
frames at view time). The composer's note says so — it names the public
link when the author-only `GET /api/highlights/{id}/publish` reports one,
and "wherever you shared it" otherwise (whether it was shared to a feed or
space isn't on the author's highlight row, so the note doesn't claim
either way).

## Retention

The author's `preferences_json.highlights.retention_days` and `.max_count`
configure the local retention policy. The `HighlightRetentionScheduler`
runs hourly:

1. Drop rows where `expires_at < now()`.
2. Per author, drop the oldest highlights beyond `max_count`.

`feed_posts.linked_highlight_id` (and the space variant) are `ON DELETE SET
NULL`, so a `highlight_share` post survives the underlying highlight's
retention — the share-card renders a "Highlight has ended" placeholder.

## Mermaid sequence — author posts a frame

```mermaid
sequenceDiagram
    autonumber
    participant U as Author<br/>(HFS A)
    participant SA as HighlightService<br/>(A)
    participant Bus as EventBus
    participant Fed as FederationService<br/>(A)
    participant B as Peer HFS B
    participant SB as HighlightService<br/>(B)
    participant V as Viewer<br/>(B)

    U->>SA: POST /api/highlights/frames<br/>{media_url, caption?, audience?}
    SA->>SA: find_or_create_today + append_frame
    SA->>Bus: publish HighlightFrameAdded
    Bus->>Fed: outbound subscriber fans out per audience
    Fed->>B: encrypted HIGHLIGHT_CREATED / HIGHLIGHT_FRAME_APPENDED
    Note over B: §24.11 pipeline:<br/>verify, decrypt, persist
    B->>SB: dispatch via _event_registry<br/>save_highlight / save_frame
    V->>SB: GET /api/highlights<br/>(viewer reads inbox)
    V->>V: tap-through<br/>POST /frames/{id}/view
    SB-->>Fed: HIGHLIGHT_FRAME_VIEWED back to A (first view only)
```

**One receipt per (frame, viewer).** `HighlightService.mark_frame_viewed`
announces a view — the `HighlightFrameViewed` bus event, and with it the
author's WS ping and, for a remote author, the `HIGHLIGHT_FRAME_VIEWED`
envelope — only when `highlight_frame_views` gets a new row; re-watching a
highlight sends nothing. The author's household applies the same rule to
an inbound receipt (an older viewer household still sends one per view).
Cost: a viewer who replays a 10-frame highlight 5 times used to send the
author's household 50 envelopes; now 10.

## Receiver rules

Highlights are sent straight from the author's home, never relayed. The
receiver drops the event with a WARNING when:

- `HIGHLIGHT_CREATED` / `HIGHLIGHT_FRAME_APPENDED` name a highlight id
  stored here with a different author, or a frame id stored here under
  a different highlight (the upserts are by id — the stored row decides);
- `HIGHLIGHT_FRAME_DELETED` names a frame not held here, or one whose
  highlight's author is not homed on the sender;
- `HIGHLIGHT_FRAME_VIEWED` / `_REACTED` / `_REACTION_REMOVED` come from a
  user not homed on the sender (unknown users included), or name a frame
  that is not part of that highlight here or not authored by one of our
  users — or come from someone the highlight never reached: the sender is
  not a household its audience resolves to, a `users` audience does not
  name this user, the author is hidden from that household (§Privacy), or
  a guardian block covers it (§CP.F2). The check is the outbound fan-out
  rule asked in reverse (`HighlightFederationOutbound.reaches`), so the two
  can't drift. The published author is the stored one, never the payload's.

A frame whose `media_url` is not a local media reference
(`api/media/<name>`, kept verbatim) is not stored: it would be an
`<img>` / `<video>` source on a third party, leaking every viewer's IP.

`tests/protocol/test_highlight_scope.py`.

## Implementation pointers

- Schema: `socialhome/migrations/0001_initial.sql` — `highlights`,
  `highlight_frames`, `highlight_frame_views`, `highlight_frame_reactions`.
  ``highlights.author_user_id`` is plain text (no FK to ``users``) so
  remote-author rows from the inbound pipeline land alongside local
  rows; same convention as ``conversation_messages.sender_user_id``.
- Domain: `socialhome/domain/highlight.py`.
- Repo: `socialhome/repositories/highlight_repo.py` —
  ``list_visible_to`` filters by audience and personal block list,
  ``save_highlight`` / ``save_frame`` are upsert helpers used by the
  inbound handlers.
- Service: `socialhome/services/highlight_service.py`. `expire_due()`
  drives the scheduler. Local writes publish ``HighlightFrameAdded`` /
  ``HighlightFrameRemoved`` / ``HighlightRemoved`` on the bus.
- Outbound federation:
  ``socialhome/services/highlight_federation_outbound.py``. Subscribes to
  the three Highlight domain events; gates each on "is the author local
  on *this* instance?" (``user_repo.get_instance_for_user(author) ==
  own``) so the same events republished from the inbound path don't
  echo back to peers. Audience routing:
  - ``all_paired`` → ``federation_repo.list_instances(status='paired')``
  - ``households`` → use the listed instance ids verbatim
  - ``users``      → resolve each user_id to its home instance via
    ``user_repo.get_instance_for_user``.
- Inbound federation: handler block in
  ``socialhome/services/federation_inbound_service.py`` registered
  by ``attach_to`` for all seven ``HIGHLIGHT_*`` event types
  (``HIGHLIGHT_CREATED`` / ``HIGHLIGHT_FRAME_APPENDED`` /
  ``HIGHLIGHT_FRAME_DELETED`` / ``HIGHLIGHT_DELETED`` are author → audience;
  ``HIGHLIGHT_FRAME_VIEWED`` / ``HIGHLIGHT_FRAME_REACTED`` /
  ``HIGHLIGHT_FRAME_REACTION_REMOVED`` are viewer → author back-channel).
  Each persists via ``highlight_repo`` and republishes the matching
  domain event so :class:`RealtimeService` fans the WS frame to the
  right local audience. The back-channel handlers verify the
  envelope's signed sender is the *viewer's* / *reactor's* home
  instance — peers can't fabricate views or reactions on behalf of
  someone else.
- Realtime push: ``socialhome/services/realtime_service.py`` —
  subscribes to the five Highlight bus events. Author-side events
  (``HighlightFrameAdded`` / ``HighlightFrameRemoved`` / ``HighlightRemoved``)
  fan ``highlight.frame_added`` / ``highlight.frame_removed`` /
  ``highlight.removed`` WS frames to the whole household so the SPA
  refetches ``/api/highlights`` (audience-filtered server-side).
  Back-channel events (``HighlightFrameViewed`` /
  ``HighlightFrameReactionChanged``) fan ``highlight.frame_viewed`` /
  ``highlight.frame_reaction_changed`` only to the *author's* WS
  sessions — unrelated household members don't need the noise.
- Routes: `socialhome/routes/highlights.py`.
- Retention scheduler:
  `socialhome/infrastructure/highlight_retention_scheduler.py` (copies the
  `_stop: asyncio.Event` template from `replay_cache_scheduler.py`).
- Frontend: `client/src/features/highlights/` (4 pages — inbox, viewer,
  composer, archive), `client/src/components/HighlightShareCard.tsx`, and
  `client/src/components/HighlightPickerDialog.tsx` for sharing into a feed.

## Spec refs

- §24.11 inbound validation pipeline (encryption-first applies).
- §25.8.21 every field encrypted unless required for routing.
- §Highlights (this page) for the author retention + audience model.
