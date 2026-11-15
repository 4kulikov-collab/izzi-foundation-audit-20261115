from django.template.loader import render_to_string
from weasyprint import HTML


def deny_resource_fetch(url, *args, **kwargs):
    raise ValueError("Внешние и локальные URL-ресурсы отключены в тестовом PDF.")


def render_release_content(snapshot):
    from .themes import for_snapshot
    from .client_data import public_view
    theme=for_snapshot(snapshot)
    # Both templates see only the same public scalar/list DTO, no model or manager context.
    return render_to_string(theme.style,{})+render_to_string(theme.body,{'snapshot':public_view(snapshot)})


def render_release_html(snapshot):
    content = render_release_content(snapshot)
    return (
        '<!doctype html><html lang="ru"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        '<meta name="robots" content="noindex,nofollow"><title>Тестовая подборка TravelHub</title>'
        '<style>body{margin:0;background:#f7f5f0;color:#203333;font-family:sans-serif;}'
        '.download-toolbar{max-width:1080px;margin:20px auto;padding:0 20px;}'
        '.download-toolbar a{display:inline-block;padding:12px 18px;color:white;background:#164f47;'
        'border-radius:9px;text-decoration:none;}@media print{html,body{background:#fff;}'
        '.download-toolbar{display:none;}}</style>'
        '</head><body><nav class="download-toolbar"><a href="pdf/">Скачать PDF</a></nav>'
        + content + '</body></html>'
    )


def render_pdf(snapshot, *, html_content=None):
    html = html_content if html_content is not None else render_release_html(snapshot)
    return HTML(string=html, url_fetcher=deny_resource_fetch).write_pdf()
