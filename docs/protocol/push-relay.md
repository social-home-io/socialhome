# Push notifications & WebRTC signalling (no GFS)

Two things this page used to describe as GFS services are not, and never
shipped that way:

1. **Push notifications** are sent by the recipient's **own household**,
   straight to the push service of the person's device (Web Push) or to
   their Home Assistant Companion app. No GFS takes part, and no other
   household does: a household notifies only its own people, about events
   it already received.
2. **WebRTC signalling** for the household-to-household DataChannel
   (`fed-v1`) travels as signed federation events between the two paired
   households (`FEDERATION_RTC_OFFER` / `_ANSWER` / `_ICE`), inside their
   sealed federation envelopes like any other event: over the HTTPS inbox,
   the mesh, or the GFS relay tier (`/gfs/envelope`, the fallback for paired
   households), where a GFS carries it only as ciphertext and can't tell it
   from any other envelope. The GFS rendezvous routes that
   once existed (`/gfs/rtc/*`) were removed without any release calling
   them (`socialhome/global_server/routes/rtc.py`). The only WebRTC the GFS
   brokers is a public highlight's anonymous viewer
   ([highlights_public.md](./highlights_public.md)).

## Scope

- **HFS**: stores its people's push subscriptions, sends their
  notifications, and signals the federation DataChannel to each paired
  household.
- **GFS**: no push, and no signalling of its own between households. It
  may carry a household's sealed envelopes on its relay tier, signalling
  included, only as ciphertext. What the GFS does is listed in
  [discovery.md](./discovery.md): the public-space directory, the
  encrypted relay of public / global space content to followers, sealed
  household-to-household envelopes (§D2b invite links, and the relay
  fallback for paired households), and public moments and highlights.

## Event types

RTC signalling (household to household, inside sealed envelopes):
`FEDERATION_RTC_OFFER`, `FEDERATION_RTC_ANSWER`, `FEDERATION_RTC_ICE`.

Push has no event type of its own. A household turns an event it received
(a DM, a post, a mention…) into a local notification for its own people.

## Flow — push to a DM recipient

```mermaid
sequenceDiagram
    autonumber
    participant A as HFS A (sender)
    participant B as HFS B (recipient's household)
    participant WP as Web Push service<br/>(the browser vendor's)
    participant D as Recipient device
    A->>B: DM_MESSAGE (signed, encrypted federation event)
    B->>B: persist, pick the recipient's<br/>push subscriptions
    B->>WP: Web Push (VAPID-signed,<br/>encrypted to the subscription)<br/>title only
    WP->>D: push notification
    Note over D: tapping it opens the SPA,<br/>which reads the message<br/>from its own household
```

In `ha` / `haos` mode the platform's push provider
(`platform/ha/providers.py` `HaPushProvider`) delivers through the
person's configured HA `notify.*` service instead (their Companion app).

## Flow — federation DataChannel bootstrap

```mermaid
sequenceDiagram
    autonumber
    participant A as HFS A (offerer)
    participant B as HFS B (answerer)
    A->>B: FEDERATION_RTC_OFFER (sealed envelope,<br/>HTTPS inbox, mesh or GFS relay)
    B->>B: build answer
    B->>A: FEDERATION_RTC_ANSWER
    par trickle ICE (both directions)
        A->>B: FEDERATION_RTC_ICE
        B->>A: FEDERATION_RTC_ICE
    end
    Note over A,B: fed-v1 DataChannel open —<br/>events ride it from now on,<br/>the HTTPS inbox stays the fallback
```

The ICE candidates are sent only after the OFFER is queued, so a candidate
never overtakes its offer (`federation/transport.py`). TURN credentials
are the household's own (`webrtc_turn_*`, see `docs/operations/turn.md`).

## Push privacy (§25.3)

A notification carries the title only. The body is omitted for DMs,
location messages and any other user-generated content. Other kinds get a
title like "New post in Family" with nothing the person wrote. The content
is read when the person opens the notification, from their own household,
over an authenticated request.

## Subscription lifecycle

- `GET /api/push/vapid_public_key`: this household's VAPID public key, for
  the browser's `pushManager.subscribe`. The key pair is generated on first
  start under the data directory.
- `POST /api/push/subscribe`: the device registers its subscription
  (endpoint and keys) with its own household.
- `DELETE /api/push/subscribe/{sub_id}`, `GET /api/push/subscriptions`:
  remove and list the person's devices.

## Implementation

- `socialhome/services/push_service.py`: Web Push sends (VAPID, via the
  optional `pywebpush`) and subscription bookkeeping.
- `socialhome/platform/adapter.py` (`PushProvider`) and
  `socialhome/platform/ha/providers.py` (`HaPushProvider`): the platform
  push path.
- `socialhome/federation/transport.py`: the `fed-v1` DataChannel and its
  `FEDERATION_RTC_*` signalling.
- `socialhome/global_server/routes/rtc.py`: what is left on the GFS side,
  the Ed25519 check of signed household → GFS requests (no signalling
  rendezvous).

## Spec references

§24.12.5 (federation-level WebRTC signalling),
§25.3 (push privacy),
§25.8.21 (encryption-first rule).
