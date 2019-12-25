import time
import string
import random
import json
import iso8601
import bottle
import datetime
import requests

from constants import TEMPORARY_FILES_DIR

def makeset(val):
    if isinstance(val, list):
        return set(val)
    elif isinstance(val, basestring):
        return set(val.split(","))
    return set()


def download_to_file(url, save_as=None):
    fn = save_as or "/tmp/" + random_string(12)
    rp = requests.get(url, stream=True)
    if rp.status_code == 200:
        with open(fn, 'wb') as fh:
            for chunk in rp.iter_content(1024):
                fh.write(chunk)    
    else:
        return None
    return fn

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

