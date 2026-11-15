"""Private read-only local UI; no role changes and no raw source/transcript export."""
from functools import wraps
from uuid import UUID
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.paginator import Paginator
from django.http import Http404, HttpResponse
from django.shortcuts import render
from django.views.decorators.http import require_GET
from presentations.views import _private
from . import director


def guarded(view):
    @wraps(view)
    def wrapped(request, *args, **kwargs):
        try:
            return view(request, *args, **kwargs)
        except PermissionDenied:
            return _private(HttpResponse('Директорский доступ не предоставлен или отозван.', status=403, content_type='text/plain; charset=utf-8'))
        except (Http404, ValueError):
            return _private(HttpResponse('Объект или фильтр недоступен.', status=404, content_type='text/plain; charset=utf-8'))
        except ValidationError:
            return _private(HttpResponse(director.ERROR, status=409, content_type='text/plain; charset=utf-8'))
    return wrapped


@login_required
@require_GET
@guarded
def overview(request):
    grants = director.grants(request.user)
    ids = {g.office_id for g in grants}
    queryset = director.scope(request.user).order_by('office_binding__office__name', '-created_at', 'id')
    managers = list(queryset.values('owner_id', 'owner__username').distinct().order_by('owner__username'))
    office = request.GET.get('office', '')
    manager = request.GET.get('manager', '')
    if office:
        office_id = UUID(office)
        if office_id not in ids:
            raise Http404
        queryset = queryset.filter(office_binding__office_id=office_id)
    if manager:
        if not manager.isdecimal() or int(manager) not in {m['owner_id'] for m in managers}:
            raise Http404
        queryset = queryset.filter(owner_id=int(manager))
    search = request.GET.get('q', '').strip()
    if len(search) > 120:
        raise Http404
    if search:
        queryset = queryset.filter(traveller_label__icontains=search)
    page = Paginator(queryset, 25).get_page(request.GET.get('page'))
    items = list(page.object_list)
    filters = request.GET.copy()
    filters.pop('page', None)
    response = _private(render(request, 'inquiries/director_list.html', {
        'page': page, 'items': items, 'offices': [g.office for g in grants], 'managers': managers,
        'chosen_office': office, 'chosen_manager': manager, 'search': search,
        'filters': filters.urlencode(),
    }))
    if not ids.issubset({g.office_id for g in director.grants(request.user)}):
        raise PermissionDenied
    if set(director.scope(request.user).filter(pk__in=[i.pk for i in items]).values_list('pk', flat=True)) != {i.pk for i in items}:
        raise PermissionDenied
    return response


@login_required
@require_GET
@guarded
def detail(request, pk):
    inquiry, rows = director.timeline(request.user, pk)
    response = _private(render(request, 'inquiries/director_detail.html', {
        'inquiry': inquiry, 'rows': rows, 'incomplete': any(not r['valid'] for r in rows),
    }))
    director.allowed(request.user, pk)
    return response


@login_required
@require_GET
@guarded
def pdf(request, pk):
    data = director.pdf(request.user, pk)
    response = HttpResponse(data, content_type='application/pdf')
    response['Content-Disposition'] = 'attachment; filename="inquiry-release.pdf"'
    return _private(response)
