import uuid
import time
import json
import bottle
import rq
import requests

import traceback

from constants import *
from backend.logger import log
from backend.storage import rdb, rdbq
from backend.util import makeset
import models.template
import models.gateway


@bottle.get(URL_ROOT + "mms/outbound/<msgid>")
@bottle.get(URL_ROOT + "mms/inbound/<msgid>")
def get_mms(msgid):
    m = MMSMessage(msgid)
    return m.as_dict() if m else \
        json_error(404, "Not found", "MMS message transaction {} not found".format(msgid))


@bottle.post(URL_ROOT + "mms/outbound/<template_id>")
def enqueue_outbound_mms(template_id):
    mj = bottle.request.json
    m = MMSMessage(template_id=template_id)

    m.direction = -1
    m.origin = mj.get("origin", m.template.origin)
    m.destination = makeset(mj.get("destination"))
    m.cc = makeset(mj.get('cc'))
    m.bcc = makeset(mj.get('bcc'))
    # make sure we have destination numbers
    if (len(m.destination) + len(m.cc) + len(m.cc)) == 0:
        return json_error(400, "Bad request", "No destinations")

    m.linked_id = mj.get('linked_id', "")
    pri = mj.get('priority', "").lower()
    m.priority = pri if pri in ACCEPTED_MESSAGE_PRIORITIES else "normal"
    m.report_url = mj.get('report_url', "")

    m.nq(mj.get('gateway'))
    m.save()
    return m.as_dict()


@bottle.post(URL_ROOT + "mms/inbound/<event:re:ack|dr|rr>/<rxid>")
def enqueue_inbound_mms_event(event, rxid):
    # expected format: dictionary with the following keys
    #    gateway: the gateway that this message needs to be sent thru
    #    message: our own message ID
    #    provider_ref: provider's original message id (X-Mms-Message-Id)
    #    status: canonical status id
    #    description: verbose description of the status
    #    applies_to: phone number(s) this status applies to; missing means applies to all
    ev = bottle.request.json
    log.info(">>>> ordered {} event for incoming MMS {}: {}".format(event, rxid, ev))
    if \
        ev.get('gateway') is None or \
        ev.get('message') is None or \
        ev.get('provider_ref') is None or \
        ev.get('status') is None or \
        (event != "ack" and ev.get('event_for') is None) or \
        (event != "ack" and ev.get('applies_to') is None) \
    :
        json_error(400, "Bad Request", "Missing required parameter")

    nums = list(ev.get('applies_to', ""))

    q_ev = rq.Queue("QEV-" + ev['gateway'], connection=rdbq)
    q_ev.enqueue_call(
        func='models.gateway.inbound_mms_event', 
        args=( 
            ev['message'], event, ev['provider_ref'],
            ev['status'], ev['description'], 
            ev.get('event_for'), list(ev.get('applies_to', "")), 
        ),
        meta={ 'retries': MAX_TX_RETRIES }
    )


# handle MM7 requests received from carrier side, could be MOs or events for MTs
@bottle.post(URL_ROOT + "mms/inbound/<gw_group>")
def mm7_inbound(gw_group):
    raw_content = bottle.request.body.read()
    log.info(">>>> {} request received, {} bytes".format(gw_group, bottle.request.headers['Content-Length']))
    log.debug("raw content: {}...".format(raw_content[:4096]))
    if len(raw_content) > 4096:
        log.debug(">>>> ... {}".format(raw_content[-256:]))

    bottle.response.content_type = "text/xml"
    mime_headers = "Mime-Version: 1.0\nContent-Type: " + bottle.request.headers['Content-Type']

    # try parsing lightly, to determine what queue to place this in
    m = email.message_from_string(mime_headers + "\n\n" + raw_content)
    try:
        if m.is_multipart():
            parts = m.get_payload()
            log.debug("handling as multipart, {} parts".format(len(parts)))
            env_content = parts[0].get_payload(decode=True)
            log.debug("SOAP envelope: {}".format(env_content))
            env = ET.fromstring(env_content)
        else:
            log.debug("handling as single part")
            env = ET.fromstring(m.get_payload(decode=True))
    except ET.ParseError as e:
        log.warning("{} failed to xml-parse the SOAP envelope: {}".format(gw_group, e))
        return bottle.HTTPResponse(status=400, body="Failed to xml-parse the SOAP envelope")

    # get the transaction tag, treat it as unique ID of the incoming message
    transaction_tag = env.find(
        "./{" + MM7_NAMESPACE['env'] + "}Header/{" + MM7_NAMESPACE['mm7'] + "}TransactionID"
    , MM7_NAMESPACE)
    if transaction_tag is None:
        s = "SOAP envelope of received request invalid, at least missing a transaction ID"
        log.warning(s)
        return bottle.HTTPResponse(status=400, body=s)
    transaction_id = transaction_tag.text.strip()

    # try to identify the message type
    mo_meta = env.find(
        "./{" + MM7_NAMESPACE['env'] + "}Body/{" + MM7_NAMESPACE['mm7'] + "}DeliverReq"
    , MM7_NAMESPACE)
    if mo_meta:
        log.debug("Incoming message is an MO")
        q_rx = rq.Queue("QRX-" + gw_group, connection=rdbq)
        q_rx.enqueue_call(
            func='models.gateway.process_mo', args=( transaction_id, mo_meta, parts[1], ), 
            job_id=transaction_id,
            meta={ 'retries': MAX_RX_RETRIES },
            ttl=30
        )
        return MM7Gateway.build_response("DeliverRsp", transaction_id, msgid, '1000')

    dr_meta = env.find(
        "./{" + MM7_NAMESPACE['env'] + "}Body/{" + MM7_NAMESPACE['mm7'] + "}DeliveryReportReq"
    , MM7_NAMESPACE)
    if dr_meta:
        log.debug("Incoming message is a DLR")
        q_ev = rq.Queue("QEV-" + gw_group, connection=rdbq)
        q_ev.enqueue_call(
            func='models.gateway.process_event', args=( transaction_id, dr_meta, ), 
            job_id=transaction_id,
            meta={ 'retries': MAX_EV_RETRIES },
            ttl=30
        )
        return MM7Gateway.build_response("DeliveryReportRsp", transaction_id, msgid, '1000')

    # handling for other MM7 requests (cancel, replace, read-reply, etc) go here 

    log.warning("Unknown or unhandled message type")
    return bottle.HTTPResponse(status=400, body="Unknown or unhandled message type")


class MMSMessage(object):

    id = None
    template = None
    provider_ref = ""    # X-MMS-Message-ID if carrier initiated
    last_tran_id = None  # last X-MMS-Transaction-ID set by carrier
    ack_at_addr = ""
    direction = 0
    gateway = ""
    gateway_id = ""
    origin = ""
    destination = set()
    cc = set()
    bcc = set()
    linked_id = ""
    priority = ""
    handling_app = ""
    reply_to_app = ""
    app_info = ""
    events_url = ""
    ack_requested = False
    dr_requested = False
    rr_requested = False
    created_ts = 0
    processed_ts = 0


    def __init__(self, message_id=None, template_id=None):
        if message_id is None:
            self.id = str(uuid.uuid4()).replace("-", "")
            self.last_tran_id = str(uuid.uuid4()).replace("-", "")
            self.created_ts = int(time.time())
            self.template = models.template.MMSMessageTemplate(template_id)
            self.save()
            rdb.expireat('mms-' + self.id, int(time.time()) + MMS_TTL)
        else:
            self.load(message_id)


    def save(self):
        rdb.hmset('mms-' + self.id, {
            'template_id': self.template.id,
            'provider_ref': self.provider_ref,
            'last_tran_id': self.last_tran_id,
            'ack_at_addr': self.ack_at_addr,
            'direction': self.direction,
            'gateway': self.gateway,
            'gateway_id': self.gateway_id,
            'origin': self.origin,
            'destination': ",".join(self.destination),
            'cc': ",".join(self.cc),
            'bcc': ",".join(self.bcc),
            'linked_id': self.linked_id,
            'priority': self.priority,
            'handling_app': self.handling_app,
            'reply_to_app': self.reply_to_app,
            'app_info': self.app_info,
            'events_url': self.events_url,
            'ack_requested': 1 if self.ack_requested else 0,
            'dr_requested': 1 if self.dr_requested else 0,
            'rr_requested': 1 if self.rr_requested else 0,
            'created_ts': self.created_ts,
            'processed_ts': self.processed_ts,
        })


    def load(self, msgid):
        md = rdb.hgetall("mms-" + msgid)
        if md:
            self.id = msgid
            self.template = models.template.MMSMessageTemplate(md['template_id'])
            self.provider_ref = md['provider_ref']
            self.last_tran_id = md['last_tran_id']
            self.ack_at_addr = md['ack_at_addr']
            self.direction = md['direction']
            self.gateway_id = md['gateway_id']
            self.gateway = md['gateway']
            self.origin = md['origin']
            l = md.get('destination', "")
            self.destination = set(l.split(",")) if len(l) > 0 else set()
            l = md.get('cc', "")
            self.cc = set(l.split(",")) if len(l) > 0 else set()
            l = md.get('bcc', "")
            self.bcc = set(l.split(",")) if len(l) > 0 else set()
            self.linked_id = md['linked_id']
            self.priority = md['priority']
            self.handling_app = md['handling_app']
            self.reply_to_app = md['reply_to_app']
            self.app_info = md['app_info']
            self.events_url = md['events_url']
            self.ack_requested = md['ack_requested'] == 1
            self.dr_requested = md['dr_requested'] == 1
            self.rr_requested = md['rr_requested'] == 1
            self.created_ts = md['created_ts']
            self.processed_ts = md['processed_ts']


    def as_dict(self):
        d = {
            'id': self.id,
            'provider_ref': self.provider_ref,
            'last_tran_id': self.last_tran_id,
            'ack_at_addr': self.ack_at_addr,
            'direction': self.direction,
            'gateway_id': self.gateway_id,
            'gateway': self.gateway,
            'origin': self.origin,
            'destination': list(self.destination),
            'cc': list(self.cc),
            'bcc': list(self.bcc),
            'linked_id': self.linked_id,
            'priority': self.priority,
            'handling_app': self.handling_app,
            'reply_to_app': self.reply_to_app,
            'app_info': self.app_info,
            'events_url': self.events_url,
            'ack_requested': self.ack_requested,
            'dr_requested': self.dr_requested,
            'rr_requested': self.rr_requested,
            'created_ts': self.created_ts,
            'processed_ts': self.processed_ts,
        }
        if self.template:
            d['template'] = self.template.as_dict()
            all_parts = []
            for pid in self.template.parts:
                part = models.template.MMSMessagePart(pid)
                if part:
                    all_parts.append(part.as_dict())
            d['template']['parts'] = all_parts
        d['events'] = []
        events = rdb.lrange('mmsev-' + self.id, 0, -1)
        for ev_json in events:
            d['events'].append(json.loads(ev_json))

        return d


    def nq(self, gateway):
        self.gateway = gateway
        q_tx = rq.Queue("QTX-" + (gateway or DEFAULT_GATEWAY), connection=rdbq)
        q_tx.enqueue_call(
            func='models.gateway.send_mms', args=( self.id, ), 
            job_id=self.id,
            meta={ 'retries': MAX_TX_RETRIES },
            ttl=30
        )
        log.info("[{}] transmission {} queued for processing".format(gateway, self.id))
        self.set_state([], "SCHEDULED")


    def set_state(self, dest, state, err="", desc="", gwid="", extra=None):

        log.debug(">>>> gateway {} posting event {} for {}".format(gwid, state, self.id))
        s = {
            'state': state,
            'code': err,
            'description': desc,
            'gateway': gwid,
            'timestamp': int(time.time())
        }
        if extra:
            pass

        s['destinations'] = dest if isinstance(dest, list) else [ dest ]
        if len(s['destinations']) == 0:
            s['destinations'] = [ "*" ]
        for d in s['destinations']:
            rdb.rpush("mmsev-" + self.id, json.dumps(s))
            rdb.expireat("mmsev-" + self.id, int(time.time()) + MMS_TTL)

        # callback to the app if necessary
        s['message'] = self.id
        q_cb = rq.Queue("QEV", connection=rdbq)
#        url_list = self.events_url.split(",") + gw.events_url.split(",")
#        for url in url_list:
#            q_cb.enqueue_call("backend.util.cb_post", ( url, json.dumps(s), ))


