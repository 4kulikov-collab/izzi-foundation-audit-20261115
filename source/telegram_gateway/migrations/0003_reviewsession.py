from django.conf import settings
from django.db import migrations,models
import django.db.models.deletion

class Migration(migrations.Migration):
    dependencies=[('telegram_gateway','0002_pollingstate'),('worksheet_reviews','0004_worksheetreview_current_source_version_and_more'),migrations.swappable_dependency(settings.AUTH_USER_MODEL)]
    operations=[migrations.AddField(model_name='updatereceipt',name='callback_ack_state',field=models.CharField(max_length=16,default='not_required')),
        migrations.CreateModel(name='ReviewSession',fields=[
            ('id',models.BigAutoField(auto_created=True,primary_key=True,serialize=False,verbose_name='ID')),
            ('gateway',models.CharField(max_length=64)),('context_hash',models.CharField(max_length=64)),
            ('revision_hash',models.CharField(max_length=64)),('nonce',models.UUIDField()),
            ('panel',models.CharField(max_length=16,default='summary')),('field_key',models.CharField(max_length=32,default='')),
            ('owner',models.ForeignKey(on_delete=django.db.models.deletion.PROTECT,to=settings.AUTH_USER_MODEL)),
            ('review',models.ForeignKey(null=True,on_delete=django.db.models.deletion.SET_NULL,to='worksheet_reviews.worksheetreview'))],
            options={'constraints':[models.UniqueConstraint(fields=('gateway','owner'),name='telegram_unique_review_session')]})]
