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
import models.message


@bottle.get(URL_ROOT + "mms/<tplid>")
def get_mms_template(tplid):
    tpl = MMSMessageTemplate(tplid)
    return tpl.as_dict() if tpl else \
        json_error(404, "Not found", "MMS message template '{}' not found".format(tplid))


@bottle.post(URL_ROOT + "mms")
def create_mms_template():
    tj = bottle.request.json
    t = MMSMessageTemplate()

    t.origin = tj.get('origin', "")
    t.show_sender = -1
    if type(tj.get('show_sender')) == bool:
        t.show_sender = 1 if tj['show_sender'] else 0
    t.subject = tj.get('subject', "")
    t.earliest_delivery = tj.get('earliest_delivery', 0)
    t.expire_after = tj.get('expire_after', 0)
    t.deliver_latest = tj.get('deliver_latest', 0)
    t.message_class = tj.get('message_class', "") if tj.get('message_class', "").lower() in ACCEPTED_MESSAGE_CLASSES else ""
    t.content_class = tj.get('content_class', "") if tj.get('content_class', "").lower() in ACCEPTED_CONTENT_CLASSES else ""
    t.charged_party = tj.get('charged_party', "") if tj.get('charged_party', "").lower() in ACCEPTED_CHARGED_PARTY else ""
    t.drm = -1
    if type(tj.get('drm')) == bool:
        t.drm = 1 if tj['drm'] else 0
    t.content_adaptation = -1
    if type(tj.get('content_adaptation')) == bool:
        t.content_adaptation = 1 if tj.get('content_adaptation') else 0
    t.can_redistribute = -1
    if type(tj.get('can_redistribute')) == bool:
        t.can_redistribute = 1 if tj.get('can_redistribute') else 0

    parts = []
    for pj in tj.get('parts', []):
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
                t.parts.insert(0, p.part_id)
            else:
                t.parts.append(p.part_id)
        elif isinstance(pj, basestring) and rdb.exists("mmspart-" + pj):
            t.parts.append(pj)

    t.save()

    mj = tj.get('send')
    if mj:
        # caller opted for the message to be sent out right away, build a transmission
        m = models.message.MMSMessage(template_id=t.id)
        m.destination = makeset(mj.get("destination"))
        m.cc = makeset(mj.get('cc'))
        m.bcc = makeset(mj.get('bcc'))
        #! add all variables here
        m.nq(mj.get('gateway', DEFAULT_GATEWAY))

    return t.as_dict()

@bottle.put(URL_ROOT + "mms/<tplid>")
def update_mms_template(tplid):
    t = MMSMessage(tplid)
    if t:
        tj = bottle.request.json
        if tj.get('origin'):
            t.origin = tj['origin']
        if type(tj.get('show_sender')) == bool:
            t.show_sender = 1 if tj['show_sender'] else 0
        if tj.get('subject'):
            t.subject = tj['subject']
        if tj.get('earliest_delivery'):
            t.earliest_delivery = tj['earliest_delivery']
        if tj.get('expire_after'):
            t.expire_after = tj['expire_after']
        if tj.get('deliver_latest'):
            t.deliver_latest = tj['deliver_latest'] 
        if tj.get('message_class', "").lower() in ACCEPTED_MESSAGE_CLASSES:
            t.message_class = tj['message_class'].lower()
        if tj.get('content_class', "").lower() in ACCEPTED_CONTENT_CLASSES:
            t.content_class = tj['content_class'].lower()
        if tj.get('charged_party', "").lower() in ACCEPTED_CHARGED_PARTY:
            t.charged_party = tj['charged_party'].lower()
        if type(tj.get('drm')) == bool:
            t.drm = 1 if tj['drm'] else 0
        if type(tj.get('content_adaptation')) == bool:
            t.content_adaptation = 1 if tj['content_adaptation'] else 0
        if type(tj.get('can_redistribute')) == bool:
            t.can_redistribute = 1 if tj['can_redistribute'] else 0

        for pj in tj.get('parts', []):
            if pj is None:
                # first part item being null means to reset the mms parts list
                t.parts = []
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
                    t.parts.insert(0, p.part_id)
                else:
                    t.parts.append(p.part_id)
            elif isinstance(pj, basestring) and rdb.exists("mmspart-" + pj):
                t.parts.append(pj)

        t.save()
        return m.as_dict()
    else:
        json_error(404, "Not found", "MMS message '{}' not found".format(tplid))


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


class MMSMessageTemplate(object):

    id = None
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


    def __init__(self, tplid=None):
        if tplid:
            self.load(tplid)
        else:
            self.id = str(uuid.uuid4()).replace("-", "")
            self.save()
            rdb.expireat('mmstpl-' + self.id, int(time.time()) + MMS_TTL)

    def save(self):
    # save to storage
        rdb.hmset('mmstpl-' + self.id, {
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

    def load(self, tplid):
    # load from storage
        tpl = rdb.hgetall('mmstpl-' + tplid)
        if tpl:
            self.id = tplid
            self.origin = tpl.get('origin', "")
            self.show_sender = tpl.get('show_sender', -1)
            self.subject = tpl.get('subject', "")
            self.earliest_delivery = tpl.get('earliest_delivery', 0)
            self.expire_after = tpl.get('expire_after', 0)
            self.deliver_latest = tpl.get('deliver_latest', 0)
            self.charged_party = tpl.get('charged_party', "")
            self.message_class = tpl.get('message_class', "")
            self.content_class = tpl.get('content_class', "")
            self.drm = tpl.get('drm', -1)
            self.content_adaptation = tpl.get('content_adaptation', -1)
            self.can_redistribute = tpl.get('can_redistribute', -1)
            self.parts = tpl.get('parts', "").split(",")

    def as_email(self):
        return None

    def as_httprq(self):
        return None

    def as_dict(self):
        ret = {
            'id': self.id,
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
                fh = open(repo(TMP_MEDIA_DIR, self.id + "-" + fn), "wb")
                fh.write(ep.get_payload(decode=True))
                fh.close()
            except Exception as e:
                return '500', "Failed saving file {} in {}: {}".format(fn, TMP_MEDIA_DIR, e)
            p.content_url = (url_prefix or (API_URL + URL_ROOT)) + self.id + "-" + fn
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

