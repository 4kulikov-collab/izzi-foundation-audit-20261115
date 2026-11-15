from django.db import migrations,models

class Migration(migrations.Migration):
    dependencies=[('presentations','0001_initial')]
    operations=[migrations.AddField(model_name='travelrequest',name='presentation_template',field=models.CharField(default='cards:1',max_length=80,verbose_name='Оформление'))]
