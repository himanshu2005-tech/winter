"""Code-produced content evidence, separate from factual and execution evidence."""
import hashlib
import json


def prepare(source, request=''):
    import report_html
    source = str(source or '').strip()
    if not source:
        raise ValueError('Report has no content')
    errors = report_html.validate_scenes(source)
    if errors:
        raise ValueError(' | '.join(errors))
    rendered = report_html.render_report(source)
    digest = hashlib.sha256(source.encode()).hexdigest()
    counts = {'tables': rendered['html'].count('<table'),
              'diagrams': rendered['html'].count('class="diagram'),
              'formulae': rendered['html'].count('class="formula')}
    return {'type': 'REPORT_PREPARED', 'ok': True,
            'output': f'In-app report compiled: {rendered["title"]}; {json.dumps(counts)}; sha256={digest}. '
                      'Content is prepared for display, not a file export or independent fact verification.',
            'report_artifact': {'title': rendered['title'], 'source': source,
                                'sha256': digest, 'counts': counts, 'state': 'prepared'},
            'original_request': str(request or '')}
