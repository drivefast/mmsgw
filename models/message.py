import time
import uuid
import json
import bottle

import traceback

from constants import *
from backend.logger import log
from backend.storage import rdb

@bottle.get("/mms_message/<msgid>")
def get_mms_message(msgid):
    m = MMSMessage(msgid)
    return m.as_dict() if m else \
        json_error(404, "Not found", "MMS message '{}' not found".format(msgid))


@bottle.post("/mms_message")
def create_mms_message():
    mj = bottle.request.json
    m = MMSMessage()

    m.content_class = mj.get('content_class', "")
    m.report_events_url = mj.get('report_events_url', "")
    m.origin = mj.get('origin', "")
    m.destination = makeset(mj.get("destination"))
    m.cc = makeset(mj.get('cc'))
    m.cc = makeset(mj.get('bcc'))
    m.subject = mj.get('subject', "")
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
        if mj.get('destination'):
            m.destination |= makeset(mj['destination'])
        if mj.get('cc'):
            m.cc |= makeset(mj['cc'])
        if mj.get('bcc'):
            m.bcc |= makeset(mj['bcc'])
        if mj.get('subject'):
            m.subject = mj['subject']
        for pj in mj.get('parts', []):
            if pj is None:
                # first part being null means to reset the mms parts list
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


def makeset(val):
    if isinstance(val, list):
        return set(val)
    elif isinstance(val, basestring):
        return set(val.split(","))
    return set()

class MMSMessage(object):

    message_id = None
    content_class = ""
    report_events_url = ""
    origin = ""
    destination = set()
    cc = set()
    bcc = set()
    subject = ""
    parts = []

    def __init__(self, msgid=None):
        if msgid:
            self.load(msgid)
        else:
            self.message_id = str(uuid.uuid4()).replace("-", "")
            self.save()
            rdb.expireat('mms-' + self.message_id, int(time.time()) + MMS_TTL)

    def save(self):
    # save to storage
        rdb.hmset('mms-' + self.message_id, {
            'content_class': self.content_class,
            'report_events_url': self.report_events_url,
            'origin': self.origin,
            'destination': ",".join(self.destination),
            'cc': ",".join(self.cc),
            'bcc': ",".join(self.bcc),
            'subject': self.subject,
            'parts': ",".join(self.parts),
        })

    def load(self, msgid):
    # load from storage
        msg = rdb.hgetall('mms-' + msgid)
        if msg:
            self.message_id = msgid
            self.content_class = msg.get('content_class', "")
            self.report_events_url = msg.get('report_events_url', "")
            self.origin = msg.get('origin', "")
            self.destination = set(msg.get('destination', "").split(","))
            self.cc = set(msg.get('cc', "").split(","))
            self.bcc = set(msg.get('bcc', "").split(","))
            self.subject = msg.get('subject', "")
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
            'destination': list(self.destination),
            'cc': list(self.cc),
            'bcc': list(self.bcc),
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

