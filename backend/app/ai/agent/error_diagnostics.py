"""Pure, bounded diagnostics from validation results; never include input or messages."""
import json
from json import JSONDecodeError

from pydantic import BaseModel, ValidationError


_CONDITIONS = {
    'missing': ('required_field_missing', 'required field'),
    'extra_forbidden': ('unexpected_field', 'only declared fields'),
    'string_type': ('wrong_type', 'string'),
    'int_type': ('wrong_type', 'integer'),
    'bool_type': ('wrong_type', 'boolean'),
    'list_type': ('wrong_type', 'array'),
    'model_type': ('wrong_type', 'object'),
    'dict_type': ('wrong_type', 'object'),
    'literal_error': ('invalid_literal', 'declared literal value'),
    'union_tag_invalid': ('invalid_discriminator', 'declared discriminator value'),
    'union_tag_not_found': ('missing_discriminator', 'required discriminator field'),
    'string_too_short': ('too_short', 'min_length'),
    'string_too_long': ('too_long', 'max_length'),
    'too_short': ('too_short', 'min_length'),
    'too_long': ('too_long', 'max_length'),
    'greater_than': ('out_of_range', 'gt'),
    'greater_than_equal': ('out_of_range', 'ge'),
    'less_than': ('out_of_range', 'lt'),
    'less_than_equal': ('out_of_range', 'le'),
    'feed_query_array': ('wrong_type', 'JSON string array'),
    'feed_query_blank': ('blank_query', 'non-whitespace query'),
    'feed_query_duplicate': ('duplicate_query', 'distinct non-empty queries'),
    'feed_query_delimiter': ('combined_queries', 'separate array entries without line/semicolon delimiters'),
    'query_blank': ('blank_query', 'non-whitespace string'),
    'content_blank': ('blank_content', 'visible non-whitespace content'),
    'feed_include_type': ('wrong_type', 'boolean include'),
    'feed_retained_limit': ('too_many_retained', 'max_retained'),
}


def summarize_validation_error(error: ValidationError, model: type[BaseModel] | None = None) -> dict:
    """Only model-declared locations and known numeric constraints are disclosed.

    Extra keys and mapping keys in loc may themselves be patient data. Resolve
    names against a trusted model schema instead of treating loc as safe text.
    Custom validator messages and input are omitted. Literal values come only
    from the trusted model schema, never the error's input-derived ctx strings.
    """
    schema = model.model_json_schema() if model is not None else {}
    definitions = schema.get('$defs', {})

    def location(loc):
        node, path = schema, ''
        for part in loc:
            while '$ref' in node:
                node = definitions.get(node['$ref'].split('/')[-1], {})
            choices = node.get('anyOf', [node])
            choices = [definitions.get(c['$ref'].split('/')[-1], {}) if '$ref' in c else c
                       for c in choices]
            # Pydantic inserts the discriminator tag in loc, including 1/0 for
            # boolean tags. It is a branch selector, not a second array index.
            discriminator = node.get('discriminator', {}).get('propertyName')
            if discriminator and 'oneOf' in node:
                branches = [definitions.get(c['$ref'].split('/')[-1], {}) if '$ref' in c else c
                            for c in node['oneOf']]
                selected = next((c for c in branches if
                    c.get('properties', {}).get(discriminator, {}).get('const') == part), None)
                if selected is None:
                    return ((path + '.<unknown>') if path else '<unknown>'), {}
                node = selected
                continue
            if type(part) is int and part >= 0 and any('items' in c for c in choices):
                path += f'[{part}]'
                node = next(c['items'] for c in choices if 'items' in c)
            else:
                match = next((c['properties'][part] for c in choices
                              if part in c.get('properties', {})), None)
                if match is None:
                    path += '.<unknown>' if path else '<unknown>'
                    break
                path += ('.' if path else '') + part
                node = match
        return path or '<root>', node

    issues = []
    for issue in error.errors(include_url=False, include_input=False, include_context=True)[:8]:
        kind = issue['type']
        field, node = location(issue['loc'])
        # Only local references and nullable branches used by these contracts.
        if 'anyOf' in node:
            options = [c for c in node['anyOf'] if c.get('type') != 'null']
            if len(options) == 1:
                node = options[0]
        while '$ref' in node:
            node = definitions.get(node['$ref'].split('/')[-1], {})
        reason, expected = _CONDITIONS.get(kind, ('validation_failed', 'satisfy field contract'))
        if kind == 'literal_error':
            values = node.get('enum', [node['const']] if 'const' in node else [])
            if values:
                expected = 'one of ' + json.dumps(values, ensure_ascii=False)
        elif kind in ('union_tag_invalid', 'union_tag_not_found'):
            tag = node.get('discriminator', {}).get('propertyName')
            if tag:
                field = (field + '.' if field != '<root>' else '') + tag
                branches = [definitions.get(c['$ref'].split('/')[-1], {}) if '$ref' in c else c
                            for c in node.get('oneOf', [])]
                values = [c['properties'][tag]['const'] for c in branches
                          if 'const' in c.get('properties', {}).get(tag, {})]
                if values:
                    expected = 'one of ' + json.dumps(values, ensure_ascii=False)
        elif kind in ('feed_include_type', 'feed_retained_limit'):
            name = 'include' if kind == 'feed_include_type' else 'decisions'
            if name in node.get('properties', {}):
                field = (field + '.' if field != '<root>' else '') + name
        if kind not in _CONDITIONS:
            kind = 'validation_error'  # Custom error types can also be input-derived.
        bound = issue.get('ctx', {}).get(expected)
        if type(bound) in (int, float):
            expected = f'{expected}={bound}'
        issues.append(dict(field=field, error_type=kind,
                           reason=reason, expected=expected))
    return {'validation_model': model.__name__ if model is not None else '<unknown>', 'issues': issues}


def summarize_json_error(error: JSONDecodeError) -> dict:
    # JSONDecodeError.msg/doc contain original input. Positions alone are safe.
    return dict(reason='invalid_json_syntax', error_type='JSONDecodeError',
                line=error.lineno, column=error.colno, position=error.pos,
                expected='valid JSON object')


def diagnostic_message(diagnostic: dict) -> str:
    """Render only the normalized output of the two interpretation functions."""
    if 'issues' in diagnostic:
        return '; '.join(f"{item['field']}: {item['reason']} ({item['expected']})"
                         for item in diagnostic['issues'])
    if 'field' in diagnostic and 'expected' in diagnostic:
        return f"{diagnostic['field']}: {diagnostic['reason']} ({diagnostic['expected']})"
    return 'valid JSON object required'
