"""Interpret the host's exact receipt and attribute it to its real destination."""
import hashlib
import re
from types import SimpleNamespace

from astrbot.core.platform.message_session import MessageSession
from .core.events.event_context import unwrap_event


def canonical_target(value, current):
    value = value or current
    if not isinstance(value, str):
        value = str(value)
    try:
        return str(MessageSession.from_str(value))
    except (ValueError, TypeError):
        if value and ':' not in value:
            try:
                original = MessageSession.from_str(current)
                return str(MessageSession(platform_name=original.platform_id,
                                          message_type=original.message_type, session_id=value))
            except (ValueError, TypeError):
                pass
    return None


def image_receipt(event, arguments, result):
    event = unwrap_event(event)
    arguments = arguments if isinstance(arguments, dict) else {}
    messages = arguments.get('messages')
    images = [m for m in messages if isinstance(m, dict) and str(m.get('type', '')).lower() == 'image'] if isinstance(messages, list) else []
    if not images:
        return None
    current = str(getattr(event, 'unified_msg_origin', '') or '')
    requested = canonical_target(arguments.get('session'), current)
    content = [result] if isinstance(result, str) else (getattr(result, 'content', None) or [])
    targets = []
    for item in content:
        text = item if isinstance(item, str) else getattr(item, 'text', '')
        match = re.fullmatch(r'Message sent to session ([^\r\n]+)', str(text).strip())
        if match:
            targets.append(canonical_target(match[1], current))
    confirmed = bool(requested and targets and all(target == requested for target in targets))
    reason = 'image_tool_receipt' if confirmed else 'receipt_target_mismatch' if targets else 'image_tool_failed'
    target = requested or current
    if target == current:
        destination = event
    else:
        # The initiating member is not a member of the destination conversation.
        destination = SimpleNamespace(
            unified_msg_origin=target, get_sender_id=lambda: '',
            get_extra=lambda k, d=None: event.get_extra(k, d),
            _visual_media_trace=getattr(event, '_visual_media_trace', '') or
                hashlib.sha256((current + '\0' + str(getattr(getattr(event, 'message_obj', None), 'message_id', ''))).encode()).hexdigest()[:24],
        )
    return {'event':destination,'same_session':target == current, 'confirmed':confirmed,
            'phase':'delivered' if confirmed else 'unknown' if targets else 'failed',
            'reason':reason,'images':images}
