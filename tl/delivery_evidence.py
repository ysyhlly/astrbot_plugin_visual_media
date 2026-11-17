"""Generation delivery needs receipts for the expected images, not a text notice."""
import hashlib
import os
from .file_uri import file_uri_to_path


def image_key(value):
    if not isinstance(value, str) or not value.strip():
        return ''
    value = value.strip()
    if value.startswith('file://'):
        path = file_uri_to_path(value)
        if path is None:return ''
        value = str(path)
    if not value.startswith(('http://', 'https://')):
        value = os.path.normpath(value)
    return hashlib.sha256(value.encode()).hexdigest()


def expected_image_keys(payload):
    if not isinstance(payload, dict):return frozenset()
    items = payload.get('items') or []
    records = [payload] + ([r for r in items if isinstance(r, dict)] if isinstance(items,list) else [])
    return frozenset(key for row in records for field in ('image_paths','image_urls')
                     for value in (row.get(field) if isinstance(row.get(field),(list,tuple)) else []) if (key := image_key(value)))


def note_confirmed_images(event, images, *, same_session):
    actual = {image_key(row.get('path') or row.get('url')) for row in images}
    actual.discard('')
    expected = getattr(event, '_visual_expected_image_keys', None)
    matched = actual if expected is None else actual.intersection(expected)
    if same_session and matched:
        seen = set(event.get_extra('visual_media_confirmed_image_keys', []))
        event.set_extra('visual_media_confirmed_image_keys', sorted(seen | matched))
    return len(matched)


def background_delivery_confirmed(event, payload):
    expected = expected_image_keys(payload)
    if not expected:
        return bool(getattr(event, '_has_send_oper', False))
    return expected.issubset(set(event.get_extra('visual_media_confirmed_image_keys', [])))
