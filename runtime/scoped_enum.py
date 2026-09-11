"""Prospective scoped enum codec; no historical caller is redirected here."""

from collections.abc import Mapping


def _validate(mapping, expected_scope):
    if not isinstance(mapping, Mapping) or set(mapping) != {'scope', 'forward', 'reverse'}:
        raise ValueError('invalid enum mapping')
    if not isinstance(expected_scope, str) or not expected_scope or mapping['scope'] != expected_scope:
        raise ValueError('enum scope mismatch')
    forward, reverse = mapping['forward'], mapping['reverse']
    if (not isinstance(forward, Mapping) or not isinstance(reverse, Mapping) or not forward
            or not all(isinstance(k, str) and k and isinstance(v, str) and v for k, v in forward.items())
            or len(set(forward.values())) != len(forward)
            or dict(reverse) != {v: k for k, v in forward.items()}):
        raise ValueError('invalid enum mapping')


def map_value(value, mapping, expected_scope):
    _validate(mapping, expected_scope)
    if not isinstance(value, str) or value not in mapping['forward']:
        raise ValueError('unknown enum value')
    return mapping['forward'][value]


def restore_value(token, mapping, expected_scope):
    _validate(mapping, expected_scope)
    if not isinstance(token, str) or token not in mapping['reverse']:
        raise ValueError('unknown enum token')
    return mapping['reverse'][token]
