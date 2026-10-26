# This module just provides a faster way to access the Square client for this
# installation.
from django.conf import settings
from django.core.cache import cache
from django.utils import timezone
from django.utils.translation import gettext as _

from datetime import datetime
from datetime import timezone as tz

import httpx
from square import Square
from square.core.api_error import ApiError
from square.environment import SquareEnvironment

api_environment = (
    SquareEnvironment.PRODUCTION if 'production' in
    str(getattr(settings, 'SQUARE_ENVIRONMENT', 'sandbox')).lower()
    else SquareEnvironment.SANDBOX
)
# The token is provided with a placeholder to avoid issues with blank bearer
# tokens. However, this must be specified in settings or Square cannot work.
api_client = Square(
    token=getattr(settings, 'SQUARE_ACCESS_TOKEN', 'django-danceschool'),
    environment=api_environment
)


# Errors from the Square client that mean "the API could not be used": an
# error response (bad credentials, unknown ID, outage) or no response at all.
SQUARE_API_ERRORS = (ApiError, httpx.HTTPError)

# After a failure, pages stop calling the API for this long, so that a page
# listing many records makes one failing call rather than one per record.
FAILURE_CACHE_KEY = 'danceschool_square_api_failure'
FAILURE_CACHE_SECONDS = 300


def describe_square_error(error):
    ''' A short, human-readable reason for a Square client exception. '''
    if isinstance(error, ApiError):
        details = [
            ': '.join(x for x in (getattr(e, 'code', None), getattr(e, 'detail', None)) if x)
            for e in (getattr(error, 'errors', None) or [])
        ]
        details = [d for d in details if d]
        if details:
            return '; '.join(details)
        return _('HTTP status %s') % error.status_code
    return _('could not connect (%s)') % type(error).__name__


def square_api_status():
    '''
    Whether the Square API can be used, as (ok, reason).  This makes no API
    call: it reports missing credentials, or a failure recorded in the last
    few minutes by record_square_failure().
    '''
    if not getattr(settings, 'SQUARE_ACCESS_TOKEN', None):
        return False, _('Square is not configured: SQUARE_ACCESS_TOKEN is not set')
    reason = cache.get(FAILURE_CACHE_KEY)
    if reason:
        return False, reason
    return True, ''


def record_square_failure(reason):
    cache.set(FAILURE_CACHE_KEY, reason, FAILURE_CACHE_SECONDS)


def clear_square_failure():
    cache.delete(FAILURE_CACHE_KEY)


def iso_timestamp_to_localtime(timestamp):
    '''
    Convert the ISO string timestamps that the Square API provides into a
    localized datetime.
    '''
    iso_formats = [
        ('%Y-%m-%dT%H:%M:%S.%fZ', tz.utc),
        ('%Y-%m-%dT%H:%M:%SZ', tz.utc),
        ('%Y-%m-%d', timezone.get_default_timezone()),
    ]
    dt = None

    for format in iso_formats:
        try:
            dt = datetime.strptime(
                timestamp, format[0]
            ).replace(tzinfo=format[1])
        except (TypeError, ValueError):
            continue
    if dt:
        return timezone.template_localtime(dt)
