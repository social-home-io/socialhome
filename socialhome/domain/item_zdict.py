"""The preset zlib dictionary of payload suite ``zlib1+dict1``.

A relayed item's compact JSON spends a large part of its compressed size
on the same keys and constant values in every item (``"author_sig"``,
``"writer_cert"``, ``"users_sig_suite":"ed25519"`` …). zlib can only
reference what it has already seen, and an item is compressed alone.
A preset dictionary (``zdict``) gives it that context up front, so each
item spends its bytes on what is new.

The dictionary is part of the suite: ``zlib1+dict1`` means "zlib level 9
with :data:`ZDICT_V1`". A change to the bytes is a new suite
(``zlib1+dict2``) and never an edit, and ``tests/domain/test_item_zdict.py``
pins the digest. It holds no data of any space, household or person: only
the item shapes with every variable value emptied, built once from a space,
author and post made up for that purpose. Pure, no I/O.

zlib reaches back at most 32 KiB, so fragments near the END of the
dictionary are the cheapest to reference. The most common shape, a member
post, comes last.
"""

from __future__ import annotations

#: Common value fragments: the UTC suffix of every timestamp and the
#: link-preview URL parts.
_FRAGMENTS: bytes = b'"+00:00""https://""api/media/"".webp"'

#: A member's comment as ``build_item_plaintext`` encodes it (every
#: variable value empty).
_COMMENT: bytes = (
    b'{"item_type":"comment","inner":{"item_type":"comment","item_target":"",'
    b'"space_id":"","post_id":"","author_user_id":"","author_pk":"",'
    b'"author_username":"","origin_instance_id":"","ts":"","comment_type":"text",'
    b'"parent_id":null,"content":"","media_url":null,"created_at":"","emoji":null,'
    b'"author_sig_suite":"ed25519","author_sig":"","writer_cert":{"cert_suite":'
    b'"ed25519","space_id":"","epoch":1,"instance_pk":"","scope":"write",'
    b'"issued_at":0,"cert_sig":"","writer_user_ids":[""],"users_sig":"",'
    b'"users_sig_suite":"ed25519"}}}'
)

#: A seed holder's relay of a post (``space_post_public`` inner), with a
#: link preview.
_RELAY_POST: bytes = (
    b'{"post_id":"","space_id":"","author_user_id":"","author_pk":"",'
    b'"author_username":"","type":"text","content":"","media_url":null,'
    b'"image_urls":[],"created_at":"","hidden_from_feed":false,'
    b'"origin_instance_id":"","author_sig":"","link_preview":{"url":"",'
    b'"title":"","description":"","site_name":"","thumbnail_url":""},'
    b'"link_preview_sig_suite":"ed25519","link_preview_sig":"","writer_cert":'
    b'{"cert_suite":"ed25519","space_id":"","epoch":1,"instance_pk":"",'
    b'"scope":"write","issued_at":0,"cert_sig":"","writer_user_ids":[""],'
    b'"users_sig":"","users_sig_suite":"ed25519"}}'
)

#: A member's own post (``space_item``), with a link preview.
_MEMBER_POST: bytes = (
    b'{"item_type":"post","inner":{"post_id":"","space_id":"",'
    b'"author_user_id":"","author_pk":"","author_username":"","type":"text",'
    b'"content":"","media_url":null,"image_urls":[],"created_at":"",'
    b'"hidden_from_feed":false,"origin_instance_id":"","item_type":"post",'
    b'"item_target":"","author_sig":"","link_preview":{"url":"","title":"",'
    b'"description":"","site_name":"","thumbnail_url":""},'
    b'"link_preview_sig_suite":"ed25519","link_preview_sig":"","writer_cert":'
    b'{"cert_suite":"ed25519","space_id":"","epoch":1,"instance_pk":"",'
    b'"scope":"write","issued_at":0,"cert_sig":"","writer_user_ids":[""],'
    b'"users_sig":"","users_sig_suite":"ed25519"}}}'
)

#: The ``zlib1+dict1`` preset dictionary. Never edit: a new dictionary is a
#: new suite.
ZDICT_V1: bytes = _FRAGMENTS + _COMMENT + _RELAY_POST + _MEMBER_POST
