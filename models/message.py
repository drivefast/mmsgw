import time
import uuid
import json
import bottle

import traceback

from constants import *
from backend.logger import log
from backend.storage import rdb
from backend.util import makeset
from models.transaction import MMSTransaction

@bottle.get("/mms_message/<msgid>")
def get_mms_message(msgid):
    m = MMSMessage(msgid)
    return m.as_dict() if m else \
        json_error(404, "Not found", "MMS message '{}' not found".format(msgid))


@bottle.post("/mms_message")
def create_mms_message():
    mj = bottle.request.json
    m = MMSMessage()

    m.report_events_url = mj.get('report_events_url', "")

    m.origin = mj.get('origin', "")
    m.show_sender = mj.get('show_sender') if type(mj.get('show_sender')) == bool else None
    m.subject = mj.get('subject', "")
    m.priority = mj.get('priority') if mj.get('priority', "").lower() in ACCEPTED_MESSAGE_PRIORITIES else ""
    m.expire_after = mj.get('expire_after', 0)
    m.deliver_latest = mj.get('deliver_latest', 0)
    m.message_class = mj.get('message_class', "") if mj.get('message_class', "").lower() in ACCEPTED_MESSAGE_CLASSES else ""
    m.content_class = mj.get('content_class', "") if mj.get('content_class', "").lower() in ACCEPTED_CONTENT_CLASSES else ""
    m.drm = mj.get('drm') if type(mj.get('drm')) == bool else None
    m.content_adaptation = mj.get('content_adaptation') if type(mj.get('content_adaptation')) == bool else None

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
            p.content_id = pj.get('content_id', "")
            p.attachment_name = pj.get('attachment_name', "")
            p.save()
            m.parts.append(p.part_id)
        elif isinstance(pj, basestring) and rdb.exists("mmspart-" + pj):
            m.parts.append(pj)

    m.save()

    tj = mj.get('send')
    if tj:
        # caller opted for the message to be sent out right away, build a transmission
        t = MMSTransaction(message=m.message_id, gateway=tj.get('gateway', DEFAULT_GATEWAY))
        t.destination = makeset(tj.get("destination"))
        t.cc = makeset(tj.get('cc'))
        t.bcc = makeset(tj.get('bcc'))
        t.nq()

    return m.as_dict()

@bottle.put("/mms_message/<msgid>")
def update_mms_message(msgid):
    m = MMSMessage(msgid)
    if m:
        mj = bottle.request.json
        if mj.get('content_class'):
            m.content_class = mj['content_class']
        if mj.get('report_events_url'):
            m.report_events_url = mj['report_events_url']
        if mj.get('origin'):
            m.origin = mj['origin']
        if type(mj.get('show_sender')) == bool:
            m.show_sender = mj.get('show_sender')
        if mj.get('subject'):
            m.subject = mj['subject']
        if mj.get('priority', "").lower() in ACCEPTED_MESSAGE_PRIORITIES:
            m.priority = mj['priority']
        if mj.get('expire_after'):
            m.expire_after = mj['expire_after']
        if mj.get('deliver_latest'):
            m.deliver_latest = mj['deliver_latest'] 
        if mj.get('message_class', "").lower() in ACCEPTED_MESSAGE_CLASSES:
            m.message_class = mj['message_class']
        if mj.get('content_class', "").lower() in ACCEPTED_CONTENT_CLASSES:
            m.content_class = mj['content_class']
        if type(mj.get('drm')) == bool:
            m.drm = mj['drm']
        if type(mj.get('content_adaptation')) == bool:
            m.content_adaptation = mj['content_adaptation']
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
                p.content_id = pj.get('content_id', "")
                p.attachment_name = pj.get('attachment_name', "")
                p.save()
                m.parts.append(p.part_id)
            elif isinstance(pj, basestring) and rdb.exists("mmspart-" + pj):
                m.parts.append(pj)

        m.save()
        return m.as_dict()
    else:
        json_error(404, "Not found", "MMS message '{}' not found".format(msgid))


@bottle.get("/mms_part/<partid>")
def get_mms_message(partid):
    p = rdb.hgetall('mmspart-' + partid)
    if p is None:
        return json_error(404, "Not found", "MMS message part '{}' not found".format(partid))
    p['part_id'] = partid
    p['base64'] = p['base64'] == 1
    return p


@bottle.post("/mms_part")
def create_mms_message():
    pj = bottle.request.json
    p = MMSMessagePart()
    p.content_type = pj.get('content_type', "")
    if p.content_type not in ACCEPTED_CONTENT_TYPES:
        return json_error(400, "Bad request", "Missing or invalid content type")    
    if len(pj.get('content', "")) > 0 and len(pj.get('content_url', "")) > 0:
        return json_error(400, "Bad request", "Either use 'content' OR 'content_url'")
    p.content = pj.get('content', "")
    p.content_url = pj.get('content_url', "")
    p.content_id = pj.get('content_id', "")
    p.attachment_name = pj.get('attachment_name', "")

    p.save()
    return p.as_dict()


class MMSMessage(object):

    message_id = None
    report_events_url = ""
    origin = ""
    show_sender = None
    subject = ""
    priority = ""
    expire_after = 0
    deliver_latest = 0
    message_class = ""
    content_class = ""
    drm = None
    content_adaptation = None
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
            'report_events_url': self.report_events_url,
            'origin': self.origin,
            'show_sender': -1 if self.show_sender is None else 1 if self.show_sender else 0,
            'subject': self.subject,
            'priority': self.priority,
            'expire_after': self.expire_after,
            'deliver_latest': self.deliver_latest,
            'message_class': self.message_class,
            'content_class': self.content_class,
            'drm': -1 if self.drm else 1 if self.drm else 0,
            'content_adaptation': self.content_adaptation if self.content_adaptation is None else 1 if self.content_adaptation else 0,
            'parts': ",".join(self.parts),
        })

    def load(self, msgid):
    # load from storage
        msg = rdb.hgetall('mmsmsg-' + msgid)
        if msg:
            self.message_id = msgid
            self.report_events_url = msg.get('report_events_url', "")
            self.origin = msg.get('origin', "")
            self.show_sender = msg.get('show_sender')
            self.subject = msg.get('subject', "")
            self.priority = msg.get('priority', "")
            self.expire_after = msg.get('expire_after', 0)
            self.deliver_latest = msg.get('deliver_latest', 0)
            self.message_class = msg.get('message_class', "")
            self.content_class = msg.get('content_class', "")
            self.drm = msg.get('drm', "")
            self.content_adaptation = msg.get('content_adaptation', "")
            self.parts = msg.get('parts', "").split(",")


    def as_email(self):
        return None

    def as_httprq(self):
        return None

    def as_dict(self):
        ret = {
            'message_id': self.message_id,
            'content_class': self.content_class,
            'report_events_url': self.report_events_url,
            'origin': self.origin,
            'subject': self.subject,
            'parts': []
        }
        for pid in self.parts:
            if pid:
                p = MMSMessagePart(pid)
                ret['parts'].append(p.as_dict())
        return ret


class MMSMessagePart(object):

    part_id = None
    content_url = ""
    content = ""
    content_id = ""
    content_type = ""
    attachment_name = ""
    base64 = False

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
            'content': self.content,
            'content_id': self.content_id,
            'content_type': self.content_type,
            'attachment_name': self.attachment_name,
            'base64': 1 if self.base64 else 0
        })

    def load(self, pid):
    # load from storage
        p = rdb.hgetall('mmspart-' + pid)
        if p:
            self.part_id = pid
            self.content_url = p.get('content_url', "")
            self.content = p.get('content', "")
            self.content_id = p.get('content_id', "")
            self.content_type = p.get('content_type', "")
            self.attachment_name = p.get('attachment_name', "")
            self.base64 = p.get('base64', "0") == 1

    def as_email_part(self):
        return None

    def as_httprq_part(self):
        return None

    def as_dict(self):
        return {
            'part_id': self.part_id,
            'content_url': self.content_url,
            'content': self.content,
            'content_id': self.content_id,
            'content_type': self.content_type,
            'attachment_name': self.attachment_name,
            'base64': self.base64
        }

