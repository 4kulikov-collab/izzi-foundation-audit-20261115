class PrivateShareResponses:
    """The same privacy headers also cover expired, revoked and invalid links."""
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        if request.path.startswith(('/s/', '/voice-notes/')):
            response['Cache-Control'] = 'no-store, private'
            response['X-Robots-Tag'] = 'noindex, nofollow, noarchive'
            response['Referrer-Policy'] = 'same-origin'
            response['X-Content-Type-Options'] = 'nosniff'
            response['X-Frame-Options'] = 'DENY'
            response['Content-Security-Policy'] = (
                "default-src 'none'; style-src 'self' 'unsafe-inline'; img-src 'self'; "
                "font-src 'self'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'"
            )
        return response
