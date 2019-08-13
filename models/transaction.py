import uuid
import time

from constants import *
from backend.logger import log
from backend.storage import rdb
from backend.util import makeset
from models.gateway import MMSGateway


@bottle.post("/mms_send/<msgid>")
def enqueue_mms_transaction(msgid):
    tj = bottle.request.json
    tx = MMSTransaction(msgid=msgid, gateway=tj.get('gateway'))

    tx.destination = makeset(tj.get("destination"))
    tx.cc = makeset(tj.get('cc'))
    tx.bcc = makeset(tj.get('bcc'))
    # make sure we have destination numbers
    if (len(tx.destination) + len(tx.cc) + len(tx.cc)) == 0:
        return json_error(400, "Bad request", "No destinations")

    tx.save()
    tx.nq()
    return tx.to_dict()


class MMSTransaction(object):

    tx_id = None
    message_id = None
    gateway = None
    gateway_id = None
    destination = set()
    cc = set()
    bcc = set()
    created_ts = 0
    sent_ts = 0
    forwarded_ts = 0
    forwarded_status = ""
    delivered_ts = 0
    delivered_status = ""
    read_reply_ts = 0
    read_reply_status = ""
    send_error = ""

    def __init__(self, txid=None, msgid=None, gateway=None):
        if txid is None:
            self.tx_id = str(uuid.uuid4()).replace("-", "")
            self.created_ts = int(time.time())
            self.message_id = msgid
            self.gateway = gateway
            self.save()
            rdb.expireat('mmstx-' + self.tx_id, int(time.time()) + MMSTX_TTL)
        else:
            self.load(txid)


    def save(self):
        rdb.hmset('mmstx-' + self.tx_id, {
            'message_id': self.message_id,
            'g`ateway': self.gateway,
            'g`ateway_id': self.gateway_id,
            'destination': ",".join(self.destination),
            'cc': ",".join(self.cc),
            'bcc': ",".join(self.bcc),
            'created_ts': self.created_ts,
            'sent_ts': self.sent_ts,
            'forwarded_ts': self.forwarded_ts,
            'forwarded_status': self.forwarded_status,
            'delivered_ts': self.delivered_ts,
            'delivered_status': self.delivered_status,
            'read_reply_ts': self.read_reply_ts,
            'read_reply_status': self.read_reply_status,
            'send_error': self.send_error,
        })


    def load(self, txid):
        tx = rdb.hgetall('mmstx-' + txid)
        if tx:
            self.tx_id = txid
            self.message_id = tx['message_id']
            self.gateway = tx['gateway']
            self.gateway_id = tx['gateway_id']
            self.destination = set(tx.get('destination', "").split(","))
            self.cc = set(tx.get('cc', "").split(","))
            self.bcc = set(tx.get('bcc', "").split(","))
            self.created_ts = tx['created_ts']
            self.sent_ts = tx['sent_ts']
            self.forwarded_ts = tx['forwarded_ts']
            self.forwarded_status = tx['forwarded_status']
            self.delivered_ts = tx['delivered_ts']
            self.delivered_status = tx['delivered_status']
            self.read_reply_ts = tx['read_reply_ts']
            self.read_reply_status = tx['read_reply_status']
            self.send_error = tx['send_error']


    def to_dict(self):
        return {
            'transaction_id': self.tx_id,
            'message_id': self.message_id,
            'gateway': self.gateway,
            'gateway_id': self.gateway_id,
            'destination': list(self.destination),
            'cc': list(self.cc),
            'bcc': list(self.bcc),
            'created_ts': self.created_ts,
            'sent_ts': self.sent_ts,
            'forwarded_ts': self.forwarded_ts,
            'forwarded_status': self.forwarded_status,
            'delivered_ts': self.delivered_ts,
            'delivered_status': self.delivered_status,
            'read_reply_ts': self.read_reply_ts,
            'read_reply_status': self.read_reply_status,
            'send_error': self.send_error,
        }

        
    def nq(self):
        # pick the appropriate gateway
        if self.gateway in GATEWAY_GROUPS:
            self.gateway_id = MMSGateway.select_from_group(self.gateway)
        elif self.gateway is not None:
            self.gateway_id = self.gateway
        else:
            self.gateway_id = DEFAULT_GATEWAY
        self.save()

        gw = MMSGateway(self.gateway_id)
        gw.q_tx.enqueue(
            func='models.gateway.send_mms', args=(self.tx_id), job_id=self.tx_id
        )


