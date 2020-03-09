import time
import uuid
import json
import bottle
import mimetypes

import traceback

from constants import *
from backend.logger import log
from backend.storage import rdb
from backend.util import makeset, repo
import models.transaction


@bottle.get(URL_ROOT + "mms_message/<msgid>")
def get_mms_message(msgid):
    m = MMSMessage(msgid)
    return m.as_dict() if m else \
        json_error(404, "Not found", "MMS message '{}' not found".format(msgid))


@bottle.post(URL_ROOT + "mms_message")
def create_mms_message():
    mj = bottle.request.json
    m = MMSMessage()

    m.origin = mj.get('origin', "")
    m.show_sender = -1
    if type(mj.get('show_sender')) == bool:
        m.show_sender = 1 if mj['show_sender'] else 0
    m.subject = mj.get('subject', "")
    m.earliest_delivery = mj.get('earliest_delivery', 0)
    m.expire_after = mj.get('expire_after', 0)
    m.deliver_latest = mj.get('deliver_latest', 0)
    m.message_class = mj.get('message_class', "") if mj.get('message_class', "").lower() in ACCEPTED_MESSAGE_CLASSES else ""
    m.content_class = mj.get('content_class', "") if mj.get('content_class', "").lower() in ACCEPTED_CONTENT_CLASSES else ""
    m.charged_party = mj.get('charged_party', "") if mj.get('charged_party', "").lower() in ACCEPTED_CHARGED_PARTY else ""
    m.drm = -1
    if type(mj.get('drm')) == bool:
        m.drm = 1 if mj['drm'] else 0
    m.content_adaptation = -1
    if type(mj.get('content_adaptation')) == bool:
        m.content_adaptation = 1 if mj.get('content_adaptation') else 0
    m.can_redistribute = -1
    if type(mj.get('can_redistribute')) == bool:
        m.can_redistribute = 1 if mj.get('can_redistribute') else 0

    parts = []
    for pj in mj.get('parts', []):
        if type(pj) is dict:
            p = MMSMessagePart()
            p.content_type = pj.get('content_type', "")
            if p.content_type not in ACCEPTED_CONTENT_TYPES:
                continue
            if len(pj.get('content', "")) > 0 and len(pj.get('content_url', "")) > 0:
                continue
            p.content = pj.get('content', "")
            p.content_url = pj.get('content_url', "")
            p.content_name = pj.get('content_name', p.part_id)
            p.save()
            if p.content_type == "application/smil":
                # need to keep the smil in first position
                m.parts.insert(0, p.part_id)
            else:
                m.parts.append(p.part_id)
        elif isinstance(pj, basestring) and rdb.exists("mmspart-" + pj):
            m.parts.append(pj)

    m.save()

    tj = mj.get('send')
    if tj:
        # caller opted for the message to be sent out right away, build a transmission
        t = models.transaction.MMSTransaction(message=m.message_id, gateway=tj.get('gateway', DEFAULT_GATEWAY))
        t.destination = makeset(tj.get("destination"))
        t.cc = makeset(tj.get('cc'))
        t.bcc = makeset(tj.get('bcc'))
        t.nq()

    return m.as_dict()

@bottle.put(URL_ROOT + "mms_message/<msgid>")
def update_mms_message(msgid):
    m = MMSMessage(msgid)
    if m:
        mj = bottle.request.json
        if mj.get('origin'):
            m.origin = mj['origin']
        if type(mj.get('show_sender')) == bool:
            m.show_sender = 1 if mj['show_sender'] else 0
        if mj.get('subject'):
            m.subject = mj['subject']
        if mj.get('earliest_delivery'):
            m.earliest_delivery = mj['earliest_delivery']
        if mj.get('expire_after'):
            m.expire_after = mj['expire_after']
        if mj.get('deliver_latest'):
            m.deliver_latest = mj['deliver_latest'] 
        if mj.get('message_class', "").lower() in ACCEPTED_MESSAGE_CLASSES:
            m.message_class = mj['message_class'].lower()
        if mj.get('content_class', "").lower() in ACCEPTED_CONTENT_CLASSES:
            m.content_class = mj['content_class'].lower()
        if mj.get('charged_party', "").lower() in ACCEPTED_CHARGED_PARTY:
            m.charged_party = mj['charged_party'].lower()
        if type(mj.get('drm')) == bool:
            m.drm = 1 if mj['drm'] else 0
        if type(mj.get('content_adaptation')) == bool:
            m.content_adaptation = 1 if mj['content_adaptation'] else 0
        if type(mj.get('can_redistribute')) == bool:
            m.can_redistribute = 1 if mj['can_redistribute'] else 0

        for pj in mj.get('parts', []):
            if pj is None:
                # first part item being null means to reset the mms parts list
                m.parts = []
                continue
            if type(pj) is dict:
                p = MMSMessagePart()
                p.content_type = pj.get('content_type', "")
                if p.content_type not in ACCEPTED_CONTENT_TYPES:
                    continue
                if len(pj.get('content', "")) > 0 and len(pj.get('content_url', "")) > 0:
                    continue
                p.content = pj.get('content', "")
                p.content_url = pj.get('content_url', "")
                p.content_name = pj.get('content_name', p.part_id)
                p.save()
                if p.content_type == "application/smil":
                    # need to keep the smil in first position
                    m.parts.insert(0, p.part_id)
                else:
                    m.parts.append(p.part_id)
            elif isinstance(pj, basestring) and rdb.exists("mmspart-" + pj):
                m.parts.append(pj)

        m.save()
        return m.as_dict()
    else:
        json_error(404, "Not found", "MMS message '{}' not found".format(msgid))


@bottle.get(URL_ROOT + "mms_part/<partid>")
def get_mms_part(partid):
    p = rdb.hgetall('mmspart-' + partid)
    if p is None:
        return json_error(404, "Not found", "MMS message part '{}' not found".format(partid))
    p['part_id'] = partid
    return p


@bottle.post(URL_ROOT + "mms_part")
def create_mms_part():
    pj = bottle.request.json
    p = MMSMessagePart()
    p.content_type = pj.get('content_type', "")
    if p.content_type not in ACCEPTED_CONTENT_TYPES:
        return json_error(400, "Bad request", "Missing or invalid content type")    
    if len(pj.get('content', "")) > 0 and len(pj.get('content_url', "")) > 0:
        return json_error(400, "Bad request", "Either use 'content' OR 'content_url'")
    p.content = pj.get('content', "")
    p.content_url = pj.get('content_url', "")
    p.content_name = pj.get('content_name', p.part_id)

    p.save()
    return p.as_dict()


class MMSMessage(object):

    message_id = None
    ascii_rendering = None
    origin = ""
    show_sender = 0
    subject = ""
    earliest_delivery = 0
    expire_after = 0
    deliver_latest = 0
    charged_party = ""
    message_class = ""
    content_class = ""
    drm = 0
    content_adaptation = 0
    can_redistribute = 0
    parts = []


    def __init__(self, msgid=None):
        if msgid:
            self.load(msgid)
        else:
            self.message_id = str(uuid.uuid4()).replace("-", "")
            self.save()
            rdb.expireat('mmsmsg-' + self.message_id, int(time.time()) + MMS_TTL)

    def save(self):
    # save to storage
        rdb.hmset('mmsmsg-' + self.message_id, {
            'origin': self.origin,
            'show_sender': self.show_sender,
            'subject': self.subject,
            'earliest_delivery': self.earliest_delivery,
            'expire_after': self.expire_after,
            'deliver_latest': self.deliver_latest,
            'charged_party': self.charged_party,
            'message_class': self.message_class,
            'content_class': self.content_class,
            'drm': self.drm,
            'content_adaptation': self.content_adaptation,
            'can_redistribute': self.can_redistribute,
            'parts': ",".join(self.parts),
        })

    def load(self, msgid):
    # load from storage
        msg = rdb.hgetall('mmsmsg-' + msgid)
        if msg:
            self.message_id = msgid
            self.origin = msg.get('origin', "")
            self.show_sender = msg.get('show_sender', -1)
            self.subject = msg.get('subject', "")
            self.earliest_delivery = msg.get('earliest_delivery', 0)
            self.expire_after = msg.get('expire_after', 0)
            self.deliver_latest = msg.get('deliver_latest', 0)
            self.charged_party = msg.get('charged_party', "")
            self.message_class = msg.get('message_class', "")
            self.content_class = msg.get('content_class', "")
            self.drm = msg.get('drm', -1)
            self.content_adaptation = msg.get('content_adaptation', -1)
            self.can_redistribute = msg.get('can_redistribute', -1)
            self.parts = msg.get('parts', "").split(",")

    def as_email(self):
        return None

    def as_httprq(self):
        return None

    def as_dict(self):
        ret = {
            'message_id': self.message_id,
            'content_class': self.content_class,
            'origin': self.origin,
            'subject': self.subject,
            'parts': []
        }
        for pid in self.parts:
            if pid:
                p = MMSMessagePart(pid)
                ret['parts'].append(p.as_dict())
        return ret

    def add_part_from_mime(self, ep, url_prefix=None):
        p = MMSMessagePart()
        p.content_name = ep['Content-Id'] or p.part_id
        p.content_type = ep.get_content_type()
        if p.content_type not in ACCEPTED_CONTENT_TYPES:
            return '406', "Content type '{}' not accepted".format(ep['Content-Type'])
        fn = ep.get_filename("") or (p.content_name + ACCEPTED_CONTENT_TYPES[p.content_type])
        if p.content_type == "application/smil" or p.content_type.startswith("text/"):
            p.content = ep.get_payload(decode=True)
        elif p.content_type.startswith("image/") or p.content_type.startswith("audio/"):
            try:
                fh = open(repo(TMP_MEDIA_DIR, self.message_id + "-" + fn), "wb")
                fh.write(ep.get_payload(decode=True))
                fh.close()
            except Exception as e:
                return '500', "Failed saving file {} in {}: {}".format(fn, TMP_MEDIA_DIR, e)
            p.content_url = (url_prefix or (API_URL + URL_ROOT)) + self.message_id + "-" + fn
        else:
            return '415', "Content type '{}' not handled".format(p.content_type)
        p.save()
        self.parts.append(p.part_id)
        return '200', ""


class MMSMessagePart(object):

    part_id = None
    content_url = ""
    content = None
    content_name = ""
    content_type = ""

    def __init__(self, pid=None):
        if pid:
            self.load(pid)
        else:
            self.part_id = str(uuid.uuid4()).replace("-", "")
            self.save()
            rdb.expireat('mmspart-' + self.part_id, int(time.time()) + MMS_TTL)

    def save(self):
    # save to storage
        rdb.hmset('mmspart-' + self.part_id, {
            'content_url': self.content_url,
            'content_name': self.content_name,
            'content_type': self.content_type,
        })
        if self.content is not None:
            rdb.hset('mmspart-' + self.part_id, 'content', self.content)
        else:
            rdb.hdel('mmspart-' + self.part_id, 'content')

    def load(self, pid):
    # load from storage
        p = rdb.hgetall('mmspart-' + pid)
        if p:
            self.part_id = pid
            self.content_url = p.get('content_url', "")
            self.content = p.get('content')
            self.content_name = p.get('content_name', "")
            self.content_type = p.get('content_type', "")

    def as_email_part(self):
        return None

    def as_httprq_part(self):
        return None

    def as_dict(self):
        return {
            'part_id': self.part_id,
            'content_url': self.content_url,
            'content': self.content,
            'content_name': self.content_name,
            'content_type': self.content_type,
        }

