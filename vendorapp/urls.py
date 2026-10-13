from django.contrib import admin
from django.urls import path, include
from django.views.generic import TemplateView

urlpatterns = [
    path('admin/', admin.site.urls),
    path('', include('core.urls')),   # also serves /media/ photos from the database
    path('service-worker.js', TemplateView.as_view(
        template_name='service-worker.js',
        content_type='application/javascript'
    )),
]