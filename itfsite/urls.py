from django.conf.urls import patterns, include, url
from django.contrib import admin

urlpatterns = patterns('',
	url(r'^$', 'itfsite.views.homepage', name='homepage'),
	url(r'^(about|legal)$', 'itfsite.views.simplepage', name='simplepage'),

	url(r'^', include('contrib.urls')),

	url(r'^admin/', include(admin.site.urls)),
)
