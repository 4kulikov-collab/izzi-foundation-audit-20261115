"""Offline synthetic proof, separate company DBs; optional client PDFs only, no runtime export."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT=Path(__file__).resolve().parents[1]
CREATE=r'''
import json,sys,socket,io,hashlib
from pathlib import Path
from unittest.mock import patch
v=json.load(sys.stdin)
def deny(*a,**kw):raise RuntimeError('Offline synthetic check only')
for obj,name in ((socket,'getaddrinfo'),(socket,'create_connection'),(socket.socket,'connect'),(socket.socket,'connect_ex'),(socket.socket,'bind')):setattr(obj,name,deny)
from stage_one_demo.ui_setup import prepare
meta=prepare(v['state'],('SYNTHETIC_MANAGER_PASSWORD_ONLY','SYNTHETIC_DIRECTOR_PASSWORD_ONLY'))
from django.contrib.auth import get_user_model
from django.test import Client
from django.core.exceptions import PermissionDenied
from django.urls import reverse
from worksheet_reviews.services import create_review
from worksheet_reviews.ocr_services import initialize_ocr,save_fields
from presentations.models import Hotel,Release,TravelRequest
from presentations.services import build_snapshot,snapshot_hash,approve_request,publish_request,select_template
from inquiries.manager import prepare as prepare_selection
from inquiries.models import Inquiry,Version
from inquiries.offers import read
from inquiries import director
from pypdf import PdfReader
owner=get_user_model().objects.get(username='demo_manager');viewer=get_user_model().objects.get(username='demo_director')
original=(Path('docs/fixtures/synthetic-travel-card.png')).read_bytes()
work=create_review(owner=owner,work_code='SYNTHETIC-THEMES',worksheet_bytes=original,worksheet_type='image/png',hotel_choices=[])
values={'party':'Двое взрослых','children_count':'0','dates':'10–17 июня 2027','budget':'200000','trip_type':'Пляжный отдых','beach':'Песчаный пляж','wishes':'Спокойный отдых'}
fields={k:{'value':value,'state':'known','source':{'kind':'manual_review','page':1,'description':'Полностью вымышленный ручной пример; OCR не выполнялся.'}} for k,value in values.items()};fields['budget']['currency']='RUB'
with patch('worksheet_reviews.ocr_services.recognize_card_photo',return_value=(fields,{'status':'synthetic_manual'})):rev=initialize_ocr(work,owner)
rev=save_fields(work,owner,rev.number,rev.content_hash,fields,confirm=True)
hotel=Hotel.objects.first()
offer={'schema':'izzi.selected-offer.v1','synthetic_only':True,'hotel_id':str(hotel.pk),'room_type':'Номер Стандарт, два отдельных спальных места','tariff':'Завтрак, без возврата','price_basis':'За двоих взрослых, 7 ночей; проживание и завтрак','base_price':'173421.13','client_price':'185000.25','currency':'RUB','selection_confirmed':True,'flight':None}
# Use the module's existing declared schema, no second contract.
from inquiries.offers import SCHEMA
offer['schema']=SCHEMA
selection=prepare_selection(actor=owner,work_id=work.pk,expected_hash=rev.content_hash,hotel_ids=[hotel.pk],traveller_label='Вымышленная поездка',offer=offer)
private=read(selection)
def issue():
 a=approve_request(selection,owner,snapshot_hash(build_snapshot(selection)))
 return publish_request(selection,owner,a.pk)
first,token=issue();frozen=(first.snapshot,first.html_content,bytes(first.pdf_bytes),first.pdf_hash)
selection=select_template(selection_id=selection.pk,actor=owner,key='brief:1',expected_hash=snapshot_hash(build_snapshot(selection)))
second,_=issue();first.refresh_from_db()
assert frozen==(first.snapshot,first.html_content,bytes(first.pdf_bytes),first.pdf_hash)
assert read(selection)==private and first.snapshot['offer']==second.snapshot['offer'] and first.snapshot['hotels']==second.snapshot['hotels']
assert Inquiry.objects.count()==1 and bytes(work.worksheet_bytes)==original
inquiry=Inquiry.objects.get();_,rows=director.timeline(viewer,inquiry.pk)
assert all(r['valid'] for r in rows) and len([r for r in rows if r.get('release')])==2
c=Client();c.force_login(owner)
if v.get('foreign'):
 f=v['foreign'];assert f['organization']!=meta['organization_id']
 for url in (reverse('presentations:preview',args=[f['selection']]),reverse('presentations:archive_pdf',args=[f['release']])):assert c.get(url).status_code==404
 assert c.post(reverse('presentations:change_template',args=[f['selection']]),{'template':'cards:1','content_hash':f['hash']}).status_code==404
 try:select_template(selection_id=f['selection'],actor=owner,key='cards:1',expected_hash=f['hash'])
 except PermissionDenied:pass
 else:raise AssertionError('Foreign selection accepted')
 try:director.timeline(viewer,f['inquiry'])
 except PermissionDenied:pass
 else:raise AssertionError('Foreign director access')
 stranger=Client();stranger.cookies['sessionid']=f['cookie'];assert stranger.get('/').status_code==302
 assert c.get(reverse('presentations:share',args=[f['token']])).status_code==404
 assert read(selection)==private and TravelRequest.objects.get(pk=selection.pk).presentation_template=='brief:1'
files=[]
for label,release in (('cards',first),('brief',second)):
 data=bytes(release.pdf_bytes);reader=PdfReader(io.BytesIO(data));text=''.join(p.extract_text() or '' for p in reader.pages)
 assert '173421.13' not in text+release.html_content and '173 421,13' not in text and 'SYNTHETIC_UI_INTERNAL_ONLY' not in release.html_content
 assert '185 000,25' in text and 'Перелёт не заявлен' in text and 'Стандарт' in text
 if v.get('output'):
  (Path(v['output'])/(label+'.pdf')).write_bytes(data)
  (Path(v['output'])/(label+'.html')).write_text(release.html_content)
 files.append({'template':release.snapshot['presentation'],'pdf_sha256':release.pdf_hash,'pages':len(reader.pages)})
print(json.dumps({'proof':{'one_inquiry':True,'two_releases':True,'director_links_verified':True,'selected_data_unchanged':True,'original_unchanged':True,'base_price_private':True,'flight_unconfirmed':True,'foreign_company_denied':bool(v.get('foreign')),'files':files},'private':{'organization':meta['organization_id'],'selection':str(selection.pk),'release':str(first.pk),'inquiry':str(inquiry.pk),'hash':snapshot_hash(build_snapshot(selection)),'cookie':c.cookies['sessionid'].value,'token':token,'pdf_hash':first.pdf_hash}}))
'''
REOPEN=r'''
import os,json,sys,socket
def deny(*a,**kw):raise RuntimeError('Offline synthetic check only')
for obj,name in ((socket,'getaddrinfo'),(socket,'create_connection'),(socket.socket,'connect'),(socket.socket,'connect_ex'),(socket.socket,'bind')):setattr(obj,name,deny)
v=json.load(sys.stdin);os.environ.update(DJANGO_SETTINGS_MODULE='config.settings',TRAVELHUB_DATA_DIR=v['state'])
import django;django.setup()
from presentations.models import Release,TravelRequest
from inquiries.models import Inquiry
release=Release.objects.get(pk=v['release'])
assert release.pdf_hash==v['pdf_hash'] and release.snapshot['presentation']['id']=='cards'
assert TravelRequest.objects.get(pk=v['selection']).presentation_template=='brief:1' and Inquiry.objects.count()==1
from django.test import Client
from django.contrib.auth import get_user_model
from django.urls import reverse
from django.core.exceptions import PermissionDenied
from presentations.services import select_template
from inquiries.director import timeline
owner=get_user_model().objects.get(username='demo_manager');viewer=get_user_model().objects.get(username='demo_director')
f=v['foreign'];c=Client();c.force_login(owner)
for url in (reverse('presentations:preview',args=[f['selection']]),reverse('presentations:archive_pdf',args=[f['release']]),reverse('presentations:share',args=[f['token']])):assert c.get(url).status_code==404
assert c.post(reverse('presentations:change_template',args=[f['selection']]),{'template':'cards:1','content_hash':f['hash']}).status_code==404
try:select_template(selection_id=f['selection'],actor=owner,key='cards:1',expected_hash=f['hash'])
except PermissionDenied:pass
else:raise AssertionError('Foreign selection accepted')
try:timeline(viewer,f['inquiry'])
except PermissionDenied:pass
else:raise AssertionError('Foreign director access')
stranger=Client();stranger.cookies['sessionid']=f['cookie'];assert stranger.get('/').status_code==302
print('unchanged and isolated')
'''


def run(output):
    output=Path(output).absolute();output.mkdir(exist_ok=False)
    env=dict(os.environ)
    for key in ('DJANGO_SETTINGS_MODULE','TRAVELHUB_DATA_DIR','PYTHONPATH'):env.pop(key,None)
    def call(code,data):
        p=subprocess.run([sys.executable,'-c',code],input=json.dumps(data),cwd=ROOT,env=env,text=True,capture_output=True,timeout=60)
        if p.returncode:raise AssertionError('Synthetic process failed: '+p.stderr[-1500:])
        return p.stdout
    with tempfile.TemporaryDirectory(prefix='izzi-theme-companies-') as tmp:
        a,b=str(Path(tmp)/'a'),str(Path(tmp)/'b')
        first=json.loads(call(CREATE,{'state':a,'output':str(output)}))
        second=json.loads(call(CREATE,{'state':b,'foreign':first['private']}))
        assert call(REOPEN,{'state':a,**first['private'],'foreign':second['private']}).strip()=='unchanged and isolated'
        first['proof']['foreign_company_denied']=True
        report={'company_a':first['proof'],'company_b':second['proof'],'restart_preserved':True,
                'network_used':False,'native_platforms_checked':False,'ocr':'synthetic manual fixture; not recognition',
                'crm':'not_synchronized','runtime_db_keys_sessions_not_exported':True}
    (output/'checks.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    return report

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--output',type=Path,required=True);p.add_argument('--synthetic-only',action='store_true',required=True)
    args=p.parse_args();print(json.dumps(run(args.output),ensure_ascii=False,indent=2))
