from django.conf import settings
from django.db import migrations,models
import django.db.models.deletion

class Migration(migrations.Migration):
    dependencies=[('telegram_gateway','0001_initial'),migrations.swappable_dependency(settings.AUTH_USER_MODEL)]
    operations=[migrations.CreateModel(name='PollingState',fields=[
        ('gateway',models.CharField(max_length=64,primary_key=True,serialize=False)),
        ('binding_hash',models.CharField(max_length=64)),
        ('next_offset',models.PositiveBigIntegerField(default=0)),
        ('owner',models.ForeignKey(on_delete=django.db.models.deletion.PROTECT,to=settings.AUTH_USER_MODEL))])]
