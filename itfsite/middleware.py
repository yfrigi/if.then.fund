from django.conf import settings

def itfsite_template_context_processor(request):
	return {
		"SITE_MODE": settings.SITE_MODE,
	}
