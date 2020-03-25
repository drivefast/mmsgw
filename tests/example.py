import bottle
import requests

from constants import *
from backend.logger import log

# this is the endpoint where your application receives transaction-related events
# the events_url setting in the gateway configuration would be handled here
@bottle.post(URL_ROOT + "example/mmsevent")
def mmsevent():
    ev = bottle.request.json
    log.debug(">>>> example application received new message event: {}".format(ev))
    return {'type': "event"}


# this is the endpoint where your application receives notifications for incoming MOs
# the mo_received_url setting in the gateway configuration would be handled here
@bottle.post(URL_ROOT + "example/mmsmo")
def mmsmo():
    m = bottle.request.json
    log.debug(">>>> example application received new message: {}".format(m))
    log.debug(">>>> id {} gateway '{}' from {} to {}: {}".format(
        m['message']['message_id'], m['gateway'],
        m['message']['origin'], m['destination'], m['message']['subject']
    ))
    log.debug(">>>> parts:")
    for p in m['message']['parts']:
        if p['content']:
            content = p['content']
        elif p['content_url']:
            content = "at" + p['content_url']
        log.debug(">>>>>>>> {} ({}): {}".format(p['content_name'], p['content_type'], content))

    # TODO: you need to set this to be the URL of your MMS gateway, that's  where you send 
    # MO processing events
    MMSGW_URL = "https://api.mmsgw.org/mmsgw/v1/"

    if m['ack_requested']:
        # we needs to calle the gateway to send an MO ACK to its peer

        # TODO: make your decision on whether you accept or reject the incoming MO, based on 
        #     whatever criteria you cere for: phone numbers, message content and size, etc
        # your acknowledgement does not have to be synchronous - you may queue it as a task
        #     and send it at a later time
        requests.post(MMSGW_URL + m['transaction_id'] + "/ack", json={
            "send_to": "", 
            "msgid": m['message']['messsage_id'], 
            "tranid": m['transaction_id'], 
            "status": "", 
            "status_desc": "", 
            #reporting_phone_num=[],
        })



#send_mo_dr: reporting_phone_num, msg_from_num, rxid, status, status_desc="", rejected_by=None
#send_mo_rr: reporting_phone_num, msg_from_num, rxid, rstatus, rstatus_desc=""



    
    return {'type': "mo"}

