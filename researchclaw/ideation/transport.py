"""Bounded JSON punctuation recovery without changing scientific scalar values."""
import json
import re
from decimal import Decimal


def parse_json_transport(text):
    raw = (text or '').strip()
    raw = re.sub(r'^```(?:json)?\s*|\s*```$', '', raw)
    start = raw.find('{')
    if start < 0:
        raise ValueError('No JSON object')
    value, end = json.JSONDecoder().raw_decode(raw, start)
    tail = raw[end:].strip()
    # A complete object followed by at most three redundant closing brackets.
    # Never append missing data or silently ignore a second response object.
    if tail and not re.fullmatch(r'[}\]]{1,3}', tail):
        raise ValueError('Unexpected content after complete JSON object')
    if not isinstance(value, dict):
        raise ValueError('JSON response must be an object')
    return value


def scalar_tokens(text):
    pattern = r'"(?:\\.|[^"\\])*"|-?\b\d+(?:\.\d+)?(?:[eE][+-]?\d+)?\b|\b(?:true|false|null)\b'
    tokens = []
    for token in re.findall(pattern, text):
        if token.startswith('"'):
            tokens.append(('string', json.loads(token)))
        elif token in ('true', 'false', 'null'):
            tokens.append(('literal', token))
        else:
            tokens.append(('number', Decimal(token)))
    return tokens


def unchanged_scalars(original, repaired):
    """Keys, strings, numbers and booleans must retain their exact order/value."""
    try:
        return scalar_tokens(original) == scalar_tokens(json.dumps(repaired, ensure_ascii=False))
    except (ValueError, ArithmeticError):
        return False
