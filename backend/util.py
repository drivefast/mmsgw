import time
import string
import random
import json
import iso8601
import bottle
import datetime


def makeset(val):
    if isinstance(val, list):
        return set(val)
    elif isinstance(val, basestring):
        return set(val.split(","))
    return set()


def isodate(iso_timestamp):
    return int(time.mktime(iso8601.parse_date(iso_timestamp).timetuple()))


def epoch(dt):
    return (dt - datetime.datetime(1970, 1, 1)).total_seconds()


def random_string(length=8):
    return "".join(random.choice(string.ascii_uppercase + string.digits) for _ in range(length))


def json_error(status_code, status_line, error_message):
    bottle.response.status = status_code
    bottle.response.content_type = "application/json"
    bottle.response.body = json.dumps({ 'error': error_message })
    return bottle.response

