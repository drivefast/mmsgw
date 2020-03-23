import uuid
import time
import bottle
import rq
import requests

import traceback

from constants import *
from backend.logger import log
from backend.storage import rdb, rdbq
from backend.util import makeset
import models.message
import models.gateway


@bottle.get(URL_ROOT + "mms/<txid>")
def get_mms_transaction(txid):
    tx = MMSTransaction(txid)
    return tx.as_dict() if tx else \
        json_error(404, "Not found", "MMS message transaction {} not found".format(txid))


@bottle.post(URL_ROOT + "mms/<rxid>/<event:re:ack|dr|rr>")
def enqueue_mms_mo_event(rxid, event):
    # expected format: dictionary with the following keys
    #    gateway: the gateway that this message needs to be sent thru
    #    mo_from: the phone number that sent the original MO
    #    transaction: our own transaction ID
    #    provider_ref: provider's original id (X-Mms-Message-Id)
    #    status: canonical status id
    #    description: verbose description of the status
    #    applies_to: phone number(s) this status applies to; missing means applies to all
    ev = bottle.request.json()
    if \
        ev.get('gateway') is None or \
        ev.get('transaction') is None or \
        ev.get('provider_ref') is None or \
        ev.get('status') is None or \
        (event != "ack" and ev.get('mo_from') is None) or \
        (event != "ack" and ev.get('applies_to') is None) \
    :
        json_error(400, "Bad Request", "Missing required parameter")

    nums = list(ev.get('applies_to', ""))

    q_ev = rq.Queue("QEV-" + ev['gateway'], connection=rdbq)
    q_ev.enqueue_call(
        func='models.gateway.mmsmo_event', 
        args=( 
            ev['transaction'], event, ev['provider_ref'],
            ev['provider_ref'], ev['status'], ev['description'], 
            ev.get('mo_from'), list(ev.get('applies_to', "")), 
        ),
        meta={ 'retries': MAX_EV_RETRIES }
    )


@bottle.post(URL_ROOT + "mms_send/<msgid>")
def enqueue_mms_transaction(msgid):
    tj = bottle.request.json
    tx = MMSTransaction(msgid=msgid)

    tx.direction = -1
    tx.destination = makeset(tj.get("destination"))
    tx.cc = makeset(tj.get('cc'))
    tx.bcc = makeset(tj.get('bcc'))
    # make sure we have destination numbers
    if (len(tx.destination) + len(tx.cc) + len(tx.cc)) == 0:
        return json_error(400, "Bad request", "No destinations")

    tx.linked_id = tj.get('linked_id', "")
    pri = tj.get('priority', "").lower()
    tx.priority = pri if pri in ACCEPTED_MESSAGE_PRIORITIES else "normal"
    tx.report_url = tj.get('report_url', "")

    tx.nq(tj.get('gateway'))
    tx.save()
    return tx.as_dict()


# handle MM7 requests received from carrier side
@bottle.post(URL_ROOT + "mm7/<gw_group>/incoming")
def mm7_incoming(gw_group):
    raw_content = bottle.request.body.read()
    log.info("{} request received, {} bytes".format(gw_group, bottle.request.headers['Content-Length']))
    log.debug("raw content: {}...".format(raw_content[:4096]))
    if len(raw_content) > 4096:
        log.debug("... {}".format(raw_content[-256:]))

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


class MMSTransaction(object):

    tx_id = None
    message = None
    provider_ref = ""    # X-MMS-Message-ID if carrier initiated
    last_tran_id = None  # last X-MMS-Transaction-ID set by carrier
    ack_at_addr = ""
    direction = 0
    gateway = ""
    gateway_id = ""
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


    def __init__(self, txid=None, msgid=None):
        if txid is None:
            self.tx_id = str(uuid.uuid4()).replace("-", "")
            self.last_tran_id = str(uuid.uuid4()).replace("-", "")
            self.created_ts = int(time.time())
            self.message = models.message.MMSMessage(msgid)
            self.save()
            rdb.expireat('mmstx-' + self.tx_id, int(time.time()) + MMSTX_TTL)
        else:
            self.load(txid)


    def save(self):
        rdb.hmset('mmstx-' + self.tx_id, {
            'message_id': self.message.message_id,
            'provider_ref': self.provider_ref,
            'last_tran_id': self.last_tran_id,
            'ack_at_addr': self.ack_at_addr,
            'direction': self.direction,
            'gateway': self.gateway,
            'gateway_id': self.gateway_id,
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


    def load(self, txid):
        txd = rdb.hgetall("mmstx-" + txid)
        if txd:
            self.tx_id = txid
            self.message = models.message.MMSMessage(txd['message_id'])
            self.provider_ref = txd['provider_ref']
            self.last_tran_id = txd['last_tran_id']
            self.ack_at_addr = txd['ack_at_addr']
            self.direction = txd['direction']
            self.gateway_id = txd['gateway_id']
            self.gateway = txd['gateway']
            l = txd.get('destination', "")
            self.destination = set(l.split(",")) if len(l) > 0 else set()
            l = txd.get('cc', "")
            self.cc = set(l.split(",")) if len(l) > 0 else set()
            l = txd.get('bcc', "")
            self.bcc = set(l.split(",")) if len(l) > 0 else set()
            self.linked_id = txd['linked_id']
            self.priority = txd['priority']
            self.handling_app = txd['handling_app']
            self.reply_to_app = txd['reply_to_app']
            self.app_info = txd['app_info']
            self.events_url = txd['events_url']
            self.ack_requested = txd['ack_requested'] == 1
            self.dr_requested = txd['dr_requested'] == 1
            self.rr_requested = txd['rr_requested'] == 1
            self.created_ts = txd['created_ts']
            self.processed_ts = txd['processed_ts']


    def as_dict(self):
        d = {
            'transaction_id': self.tx_id,
            'provider_ref': self.provider_ref,
            'last_tran_id': self.last_tran_id,
            'ack_at_addr': self.ack_at_addr,
            'direction': self.direction,
            'gateway_id': self.gateway_id,
            'gateway': self.gateway,
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
        if self.message:
            d['message'] = self.message.as_dict()
            all_parts = []
            for pid in self.message.parts:
                part = models.message.MMSMessagePart(pid)
                if part:
                    all_parts.append(part.as_dict())
            d['message']['parts'] = all_parts
        d['events'] = []
        ev_keys = rdb.lrange('mmstx-' + self.tx_id + '-events', 0, -1)
        for ev_key in ev_keys:
            ev = rdb.hgetall(ev_key)
            if ev:
                d['events'].append(ev)

        return d


    def nq(self, gateway):
        self.gateway = gateway
        q_tx = rq.Queue("QTX-" + (gateway or DEFAULT_GATEWAY), connection=rdbq)
        q_tx.enqueue_call(
            func='models.gateway.send_mms', args=( self.tx_id, ), 
            job_id=self.tx_id,
            meta={ 'retries': MAX_TX_RETRIES },
            ttl=30
        )
        log.info("[{}] transmission {} queued for processing".format(gateway, self.tx_id))
        self.set_state([], "SCHEDULED")


    def set_state(self, dest, state, err="", desc="", gwid="", provider_id=None, extra=None):

        s = {
            'state': state,
            'code': err,
            'description': desc,
            'gateway': gwid,
            'timestamp': int(time.time())
        }
        if provider_id is not None:
            s['provider_id'] = provider_id
        if extra:
            s.update(extra)

        if len(dest):
            for d in dest:
                k = "mmstx-stat-" + self.tx_id + "-" + d
                rdb.hmset(k, s)
                rdb.expireat(k, int(time.time()) + MMSTX_TTL)
        else:
            k = "mmstx-stat-" + self.tx_id
            rdb.hmset(k, s)
            rdb.expireat(k, int(time.time()) + MMSTX_TTL)
        rdb.rpush('mmstx-' + self.tx_id + '-events', k)
        rdb.expireat('mmstx-' + self.tx_id + '-events', int(time.time()) + MMSTX_TTL)

        # callback to the app if necessary
        s['transaction_id'] = self.tx_id
        if len(dest):
            s['destinations'] = dest
        q_cb = rq.Queue("QEV", connection=rdbq)
#        url_list = self.events_url.split(",") + gw.events_url.split(",")
#        for url in url_list:
#            q_cb.enqueue_call("backend.util.cb_post", ( url, json.dumps(s), ))



