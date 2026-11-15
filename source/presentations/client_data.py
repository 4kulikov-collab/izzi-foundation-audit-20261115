"""Public data contract, independent of presentation choice. Never pass ORM/private context."""
from django.core.exceptions import ValidationError


def text(value):
    if value is not None and type(value) is not str:raise ValidationError('Некорректное клиентское поле.')
    return value


def strings(value):
    if type(value) is not list or any(type(x) is not str for x in value):raise ValidationError('Некорректный клиентский список.')
    return list(value)


def public_view(snapshot):
    result={key:text(snapshot.get(key,'')) for key in ('title','traveller_summary','requirements')}
    result['schema']='izzi.presentation-data.v1'
    result['hotels']=[]
    for hotel in snapshot.get('hotels',[]):
        entry={key:text(hotel.get(key,'')) for key in ('name','summary','source_reference')}
        entry.update({key:strings(hotel.get(key,[])) for key in ('pros','limitations')})
        result['hotels'].append(entry)
    offer=snapshot.get('offer')
    if offer is not None:
        entry={key:text(offer.get(key)) for key in ('status','room_type','tariff','price_basis','client_price','client_price_display','currency')}
        entry['missing']=strings(offer.get('missing',[]))
        entry['flight']={'details':text(offer['flight'].get('details'))} if offer.get('flight') else None
        imported=offer.get('imported')
        if imported:
            entry['imported']={key:text(imported.get(key)) for key in ('hotel_name','meal','departure_date','occupancy','observed_at','source_label')}
            if type(imported.get('nights')) is not int:raise ValidationError('Некорректное количество ночей.')
            entry['imported']['nights']=imported['nights']
        result['offer']=entry
    return result
