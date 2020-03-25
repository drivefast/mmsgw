import time
import string
import random
import json
import iso8601
import bottle
import datetime
import requests
import os, sys
if sys.version_info.major < 3:
    from urllib import url2pathname
else:
    from urllib.request import url2pathname
from backend.logger import log


def cb_post(url, jdata):
    log.info(">>>> [callback] POSTing to {}: {}".format(url, jdata))
    rq = requests.post(url, json=json.loads(jdata))
    if rq.status_code >= 400:
        log.warning(">>>> [callback] POSTing to {} failed with status {}: {}"
            .format(url, rq.status_code, rq.text)
        )


class FileSchemeAdapter(requests.adapters.BaseAdapter):
    # adapter to allow the requests to GET from a file:// url

    @staticmethod
    def _chkpath(method, path):
        if method.lower() in ('put', 'delete'):
            return 501, "Not Implemented" 
        elif method.lower() not in ('get', 'head'):
            return 405, "Method Not Allowed"
        elif os.path.isdir(path):
            return 400, "Path Not A File"
        elif not os.path.isfile(path):
            return 404, "File Not Found"
        elif not os.access(path, os.R_OK):
            return 403, "Access Denied"
        else:
            return 200, "OK"

    def send(self, req, **kwargs):
        path = os.path.normcase(os.path.normpath(url2pathname(req.path_url)))
        response = requests.Response()

        response.status_code, response.reason = self._chkpath(req.method, path)
        if response.status_code == 200 and req.method.lower() != 'head':
            try:
                response.raw = open(path, 'rb')
            except (OSError, IOError) as err:
                response.status_code = 500
                response.reason = str(err)

        if isinstance(req.url, bytes):
            response.url = req.url.decode('utf-8')
        else:
            response.url = req.url

        response.request = req
        response.connection = self

        return response

    def close(self):
        pass


def download_to_file(url, save_as=None):
    fn = save_as or "/tmp/" + random_string(12)
    rq_session = requests.session()
    rq_session.mount('file://', FileSchemeAdapter())
    rp = rq_session.get(url, stream=True)
    if rp.status_code == 200:
        with open(fn, 'wb') as fh:
            for chunk in rp.iter_content(4096):
                fh.write(chunk)    
        return fn
    else:
        return None


def find_in_dict(d, k):
    if k in d: return d[k]
    for kk, v in d.items():
        if isinstance(v, dict):
            i = find_in_dict(v, k)
            if i is not None:
                return i


def repo(path, fn):
    try:
        if not os.path.isdir(path + fn[:2]):
            os.mkdir(path + fn[:2])
        return path + fn[:2] + "/" + fn
    except Exception as e:
        return None


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
    return "".join(random.choice(string.ascii_uppercase + string.digits) 
        for _ in range(length)
    )


def json_error(status_code, status_line, error_message):
    bottle.response.status = status_code
    bottle.response.content_type = "application/json"
    bottle.response.body = json.dumps({ 'error': error_message })
    return bottle.response

